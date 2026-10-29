"""Crop core: recursive scan, reversible naming, padding, atomic writes.

The tool crops a fixed size rectangle out of every image of a folder
tree and writes each crop flat into an output directory:
<output>/<name>, never into a subdirectory. The source tree is only
read: no file there is modified, moved or deleted, and no json side
car is ever written - the file name of a crop *is* its record.

Scanning rules: one directory contributes its files first, in natural
order, then its subdirectories in natural order, each one visited
depth first. The flat list is the visual order of the folder tree of
the window and the order the A/D navigation follows. An
unreadable directory is skipped, a symbolic link to a directory is
never followed (a link cycle can not make the scan run forever) and
the output tree is skipped as a whole when it lives inside the scanned
tree.

Naming rules:

* a crop is named
  <stem>__x<X>_y<Y>_w<W>_h<H>__px<PX>_py<PY>[__<seq>].<ext>, with
  <stem> the stem of the source file, X/Y the top left corner of the
  crop inside the source, W/H its size and PX/PY the width of the
  padding band that was really applied on the left/top side;
* the sequence suffix is absent for the first crop of a region and
  __2, __3, ... for the following ones, so a name never collides and
  an existing file is never overwritten;
* the stem is greedy when a name is parsed back: the rightmost
  coordinate block wins, so a crop of a crop still resolves to its
  real source stem;
* every number is written and read as an ASCII decimal, so a full
  width digit can never make the parser raise.

The crop is composed at its W×H size and then, when the saved size
differs, resampled to exactly that size with a fixed Lanczos filter
before it is encoded. An axis of the saved size that is zero follows
the crop axis, so the default pair (0, 0) keeps the composed crop as
it is. The saved size is a rendering parameter and, like the fill
value, it is not encoded into the file name.

Writing is atomic per crop: the bytes go to <name>.part first and are
moved over the final name with os.replace once the encoder is done. A
failure leaves the .part file in place on purpose and deletes nothing.

The module carries no GUI dependency on purpose: the whole file layer
is unit testable without a widget.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import PIL.Image

__all__ = [
    "CROP_MARK",
    "DEFAULT_SAVE",
    "FILL",
    "IMAGE_EXTS",
    "MAX_CROP_PIXELS",
    "MAX_IMAGE_PIXELS",
    "MAX_NAME_ATTEMPTS",
    "MAX_NAME_BYTES",
    "OUTPUT_SUBDIR",
    "PART_SUFFIX",
    "PASSTHROUGH_MODES",
    "RESAMPLE",
    "SAVE_FORMATS",
    "SIZE_MAX",
    "CropError",
    "CropRecord",
    "CropResult",
    "ImageEntry",
    "build_crop_name",
    "crop_count",
    "crop_image",
    "crop_name_re",
    "default_output_dir",
    "delete_crops",
    "natural_key",
    "output_extension",
    "parse_crop_name",
    "program_dir",
    "resolve_save_size",
    "save_format",
    "scan_crops",
    "scan_directory",
]

#: Image extensions the scanner accepts, compared in lower case.
IMAGE_EXTS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
    ".gif",
)

#: Upper bound of the width, height and padding spin boxes.
SIZE_MAX = 9999

#: Largest accepted source image, in pixels (decompression bomb).
MAX_IMAGE_PIXELS = 100_000_000

#: Largest accepted crop, in pixels.
MAX_CROP_PIXELS = 100_000_000

#: How many sequence numbers one crop may try before it gives up.
MAX_NAME_ATTEMPTS = 1000

#: Largest accepted crop file name, in UTF-8 bytes.
MAX_NAME_BYTES = 240

#: Marker that opens the coordinate block of a crop name.
CROP_MARK = "__x"

#: Subdirectory of the program directory the default output uses.
OUTPUT_SUBDIR = "crop"

#: Suffix of the temporary file a crop is written to first.
PART_SUFFIX = ".part"

#: Fixed background value. It is neither exposed nor persisted nor
#: encoded into a file name.
FILL = 0

#: Modes that are kept as they are when a crop is composed.
PASSTHROUGH_MODES = ("L", "RGB", "RGBA", "I;16", "I;16B", "I;16L")

#: Resampling filter of the saved size. It is fixed for both
#: directions on purpose: the tool exposes the target size, not the
#: filter, so the same source and the same size give the same bytes.
try:
    RESAMPLE = PIL.Image.Resampling.LANCZOS
except AttributeError:  # Pillow < 9.1
    RESAMPLE = PIL.Image.LANCZOS

#: Source extension (lower case) -> (output extension, PIL format,
#: allowed modes). None means every mode of that format is accepted.
SAVE_FORMATS = {
    ".jpg": (".jpg", "JPEG", ("L", "RGB")),
    ".jpeg": (".jpeg", "JPEG", ("L", "RGB")),
    ".png": (".png", "PNG", None),
    ".bmp": (".bmp", "BMP", ("L", "RGB", "RGBA")),
    ".webp": (".webp", "WEBP", ("RGB", "RGBA")),
    ".tif": (".tif", "TIFF", None),
    ".tiff": (".tiff", "TIFF", None),
}

#: Format of a source extension the tool does not know.
DEFAULT_SAVE = (".png", "PNG", None)

#: Crop name template. The stem group is greedy on purpose (the
#: rightmost coordinate block wins) and every number is an ASCII
#: decimal: \d would also match a full width digit and int() would
#: then raise inside a GUI slot.
crop_name_re = re.compile(
    r"^(?P<stem>.+)"
    + re.escape(CROP_MARK)
    + r"(?P<x>[0-9]+)_y(?P<y>[0-9]+)_w(?P<w>[0-9]+)_h(?P<h>[0-9]+)"
    + r"__px(?P<pad_x>[0-9]+)_py(?P<pad_y>[0-9]+)"
    + r"(?:__(?P<seq>[0-9]+))?\.(?P<ext>[A-Za-z0-9]+)$"
)


class CropError(Exception):
    """A crop could not be read, composed, named or written.

    The message is meant for the user and is Chinese. It may carry the
    full path of the .part file or of the final crop.
    """


@dataclass(frozen=True)
class ImageEntry:
    """One image of the scanned source tree.

    path is the path of the file, name its base name and rel_dir the
    relative folder it sits in, '' for the root of the scanned tree.
    The scan covers the whole tree, depth first. rel_dir feeds the
    folder tree of the window and the automatic jump only: a crop
    never mirrors it, every crop lands flat in the output directory.
    """

    path: str
    name: str
    rel_dir: str = ""


@dataclass(frozen=True)
class CropRecord:
    """Everything one crop file name encodes, plus its path.

    source_stem is the stem of the source image, x/y the top left
    corner of the crop inside that source and w/h its size. pad_x and
    pad_y are the padding widths that were really applied, seq is 1
    for the first crop of a region and >= 2 for the following ones and
    ext is the output extension without the leading dot. Every field
    but path can be read back from the file name alone.
    """

    path: str
    source_stem: str
    x: int
    y: int
    w: int
    h: int
    pad_x: int
    pad_y: int
    seq: int
    ext: str


@dataclass(frozen=True)
class CropResult:
    """The outcome of one crop: the file, its record and its image.

    size is the size of the file that was written, so it is the saved
    size after the optional resampling. mode is the mode of the image
    that was handed to the encoder, so it is the mode a reader of the
    file sees after the format of the source has been honoured.
    """

    path: str
    record: CropRecord
    size: Tuple[int, int]
    mode: str


def natural_key(value: str) -> List[Tuple[int, object]]:
    """Sort key that keeps image_2 before image_10.

    Only decimal digits open a numeric run. A superscript such as the
    two of a2 passes str.isdigit() but int() rejects it with a
    ValueError, so it is compared as the ordinary character it is.
    That matters because the scan runs inside a GUI slot, where an
    uncaught exception would abort the whole application.

    Every chunk is a (rank, value) pair: a numeric run is (0, int)
    and a single character is (1, str). The rank keeps the chunks
    comparable, so a folder that mixes 1.jpg with a.jpg sorts
    instead of raising TypeError on int < str; digits rank before
    letters, the order plain strings already have.
    """

    chunks: List[Tuple[int, object]] = []
    buffer = ""
    for char in value:
        if char.isdecimal():
            buffer += char
        else:
            if buffer:
                chunks.append((0, int(buffer)))
                buffer = ""
            chunks.append((1, char.lower()))
    if buffer:
        chunks.append((0, int(buffer)))
    return chunks


def program_dir() -> str:
    """Return the directory the application runs from.

    A frozen build reports the folder of its executable. A run from
    source reports the repository root, the folder that holds the
    anylabeling package. The value is fetched on every call, so the
    default output directory can never hide a stale path.
    """

    if getattr(sys, "frozen", False):
        executable = getattr(sys, "executable", "")
        if executable:
            return os.path.dirname(os.path.abspath(executable))
    package = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(
        os.path.join(package, os.pardir, os.pardir, os.pardir)
    )


def default_output_dir() -> str:
    """Return the output directory of a run that has none configured.

    The crops land in a crop/ subdirectory of the program directory,
    so a run never drops its files straight into the folder the
    application lives in. The program directory is fetched on every
    call: the default is deliberately never cached and never written
    back.
    """

    return os.path.join(program_dir(), OUTPUT_SUBDIR)


def output_extension(source_path: str) -> str:
    """Return the extension a crop of source_path is written with."""

    return save_format(source_path)[0]


def save_format(
    source_path: str,
) -> Tuple[str, str, Optional[Tuple[str, ...]]]:
    """Return (extension, PIL format, allowed modes) of a source path.

    An unknown extension falls back to DEFAULT_SAVE, so a gif is
    written as a png.
    """

    ext = os.path.splitext(source_path)[1].lower()
    return SAVE_FORMATS.get(ext, DEFAULT_SAVE)


def resolve_save_size(
    crop_width,
    crop_height,
    save_width=0,
    save_height=0,
) -> Tuple[int, int]:
    """Return the exact size a crop is written with.

    An axis of zero follows the crop axis, so the default pair (0, 0)
    saves the composed crop untouched. A negative or a non-integer
    value, and a saved size above MAX_CROP_PIXELS, are a CropError:
    none of them can come from the spin boxes of the window, and a
    silent fallback would write a file of the wrong size.
    """

    try:
        want_width = int(save_width)
        want_height = int(save_height)
    except (TypeError, ValueError) as exc:
        raise CropError("保存尺寸不是整数：{}".format(exc)) from exc
    if want_width < 0 or want_height < 0:
        raise CropError(
            "保存尺寸不能为负：{}x{}".format(want_width, want_height)
        )
    out_width = want_width or int(crop_width)
    out_height = want_height or int(crop_height)
    if out_width * out_height > MAX_CROP_PIXELS:
        raise CropError(
            "保存尺寸过大：{}x{} 超过上限 {} 像素".format(
                out_width, out_height, MAX_CROP_PIXELS
            )
        )
    return out_width, out_height


def _same_dir(left: str, right: str) -> bool:
    """Return True when two paths name the very same directory."""

    if not left or not right:
        return False
    try:
        return os.path.samefile(left, right)
    except OSError:
        pass
    try:
        return os.path.realpath(left) == os.path.realpath(right)
    except OSError:
        return False


def _inside_output(path: str, root: str) -> bool:
    """Return True when a real path lives inside a directory tree.

    The comparison runs on real paths and on path components, so a
    sibling whose name only starts with the same text is not inside
    the root. The root itself counts as inside.
    """

    if not path or not root:
        return False
    try:
        candidate = os.path.realpath(path)
        base = os.path.realpath(root)
        common = os.path.commonpath([candidate, base])
    except (OSError, ValueError):
        return False
    return common == base


def build_crop_name(
    stem: str,
    x: int,
    y: int,
    w: int,
    h: int,
    pad_x: int,
    pad_y: int,
    out_ext: str,
    seq: int = 1,
) -> str:
    """Compose the reversible file name of one crop.

    out_ext keeps its leading dot. The sequence suffix is omitted for
    seq <= 1 and appended as __<seq> from 2 on.
    """

    name = "{}__x{}_y{}_w{}_h{}__px{}_py{}".format(
        stem, x, y, w, h, pad_x, pad_y
    )
    if seq and seq >= 2:
        name += "__{}".format(seq)
    return name + out_ext


def parse_crop_name(
    name: str, directory: str = ""
) -> Optional[CropRecord]:
    """Read a crop file name back into a record.

    name is a base name; a name that does not carry the template
    yields None. When directory is not empty the path of the record is
    that directory joined with name.
    """

    match = crop_name_re.match(name)
    if match is None:
        return None
    seq = match.group("seq")
    path = os.path.join(directory, name) if directory else name
    return CropRecord(
        path=path,
        source_stem=match.group("stem"),
        x=int(match.group("x")),
        y=int(match.group("y")),
        w=int(match.group("w")),
        h=int(match.group("h")),
        pad_x=int(match.group("pad_x")),
        pad_y=int(match.group("pad_y")),
        seq=int(seq) if seq else 1,
        ext=match.group("ext"),
    )


def _is_own_crop(name: str, directory: str, output_dir: str) -> bool:
    """Return True when a file is a crop this tool wrote before.

    A file is one of ours when its name carries the template and it
    sits inside the output directory tree. A crop is written flat into
    the output root, but the whole tree is still accepted, so that a
    template file an older build left in a subdirectory is never
    scanned back as a source. Excluding the whole output directory by
    path would be wrong: its default is the current working directory,
    which usually holds ordinary images too, so only template names
    are excluded.
    """

    if not output_dir or parse_crop_name(name) is None:
        return False
    return _inside_output(directory, output_dir)


def _scan_tree(
    directory: str,
    rel_dir: str,
    output_dir: str,
    skip_output: bool,
    entries: List[ImageEntry],
) -> None:
    """Append the images of one directory tree in navigation order.

    One directory contributes its files first, in natural order, then
    its subdirectories, also in natural order, each one visited depth
    first. An unreadable directory is skipped instead of aborting the
    scan. Symbolic links to directories are never followed, so a link
    cycle can not make the scan run forever.
    """

    try:
        with os.scandir(directory) as scan:
            items = list(scan)
    except OSError:
        return
    files: List[str] = []
    folders: List[str] = []
    for item in items:
        try:
            if item.is_file():
                if not item.name.lower().endswith(IMAGE_EXTS):
                    continue
                if _is_own_crop(item.name, directory, output_dir):
                    continue
                files.append(item.name)
            elif item.is_dir(follow_symlinks=False):
                folders.append(item.name)
        except OSError:
            continue
    files.sort(key=natural_key)
    folders.sort(key=natural_key)
    for name in files:
        entries.append(
            ImageEntry(os.path.join(directory, name), name, rel_dir)
        )
    for name in folders:
        child = os.path.join(directory, name)
        if skip_output and _same_dir(child, output_dir):
            continue
        child_rel = os.path.join(rel_dir, name) if rel_dir else name
        _scan_tree(child, child_rel, output_dir, skip_output, entries)


def scan_directory(
    directory: str, output_dir: str = ""
) -> List[ImageEntry]:
    """List every image of one directory tree in navigation order.

    Extensions are compared in lower case. One directory yields its
    files first (natural order), then its subdirectories (natural
    order), depth first, so the flat list is the visual order of the
    folder tree of the window and the order of the automatic jump
    after a crop. Every entry carries its rel_dir, the relative folder
    of its file, '' for the root of the scanned tree; that folder only
    feeds the folder tree of the window and the automatic jump, a crop
    never mirrors it.

    Files that carry the crop template and sit inside output_dir are
    skipped, because they are the crops of an earlier run. When
    output_dir lives inside the scanned tree its whole subtree is
    skipped as well, so the crops of this run are never scanned back
    as sources. A directory that is missing or unreadable yields an
    empty list (an unreadable subdirectory is simply skipped).
    """

    entries: List[ImageEntry] = []
    if not directory:
        return entries
    skip_output = False
    if output_dir:
        skip_output = _inside_output(output_dir, directory) and not (
            _same_dir(output_dir, directory)
        )
    _scan_tree(directory, "", output_dir, skip_output, entries)
    return entries


def scan_crops(output_dir: str) -> Dict[str, List[CropRecord]]:
    """Group every crop of an output directory by source stem.

    Only the root of output_dir is read, because a crop is always
    written flat into it; a subdirectory is ignored, exactly like a
    file whose name does not carry the template. The key is the stem
    of the source image. Every group is sorted in natural order of the
    file name, which keeps __2 before __10. A directory that is
    missing or unreadable yields an empty mapping.

    The folder of the source is not part of the key: two sources with
    the same stem in different folders of the scanned tree share one
    group, their marks, their badge and their deletion family. The
    output directory holds no identity beyond the crop name, so the
    only way to keep such sources apart is a separate output
    directory per input folder - a documented trade-off of the flat
    output layout.
    """

    groups: Dict[str, List[CropRecord]] = {}
    if not output_dir:
        return groups
    try:
        with os.scandir(output_dir) as scan:
            items = list(scan)
    except OSError:
        return groups
    names: List[str] = []
    for item in items:
        try:
            if not item.is_file():
                continue
        except OSError:
            continue
        names.append(item.name)
    for name in sorted(names, key=natural_key):
        record = parse_crop_name(name, output_dir)
        if record is None:
            continue
        groups.setdefault(record.source_stem, []).append(record)
    return groups


def crop_count(output_dir: str, stem: str) -> int:
    """Return how many crops of one source stem the output holds.

    A crop is always written flat into the output root, so the count
    does not need to know where its source sits in the scanned tree.
    """

    return len(scan_crops(output_dir).get(stem, ()))


def _read_source(source_path: str):
    """Load a source image and detach it from its file.

    The size guard reads MAX_IMAGE_PIXELS as a module attribute, so a
    test can lower it. A bomb, an unreadable or a truncated file is
    reported as a CropError.
    """

    try:
        with PIL.Image.open(source_path) as handle:
            width, height = handle.size
            if width * height > MAX_IMAGE_PIXELS:
                raise CropError(
                    "图片过大：{}x{} 超过上限 {} 像素".format(
                        width, height, MAX_IMAGE_PIXELS
                    )
                )
            handle.load()
            return handle.copy()
    except PIL.Image.DecompressionBombError as exc:
        raise CropError("图片过大：{}".format(exc)) from exc
    except (OSError, ValueError) as exc:
        raise CropError("图片无法读取：{}".format(exc)) from exc


def _working_mode(mode: str) -> str:
    """Return the mode a crop of a source in mode is composed in."""

    if mode in PASSTHROUGH_MODES:
        return mode
    direct = {"1": "L", "P": "RGB", "PA": "RGBA", "LA": "RGBA"}.get(mode)
    if direct is not None:
        return direct
    if "A" in mode:
        return "RGBA"
    return "RGB"


def _fill_value(mode: str, fill: int = FILL):
    """Return the background value of one mode.

    Alpha is 255, so a bar next to a transparent source is opaque
    black instead of transparent black.
    """

    if mode == "RGBA":
        return (fill, fill, fill, 255)
    if mode == "L" or mode.startswith("I;16"):
        return fill
    return (fill, fill, fill)


def _clamp_pad(raw, limit: int) -> int:
    """Return a padding width inside 0..limit, never raising."""

    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(value, limit))


def _crop_size(width, height) -> Tuple[int, int]:
    """Validate the requested crop size against MAX_CROP_PIXELS."""

    try:
        crop_w = int(width)
        crop_h = int(height)
    except (TypeError, ValueError) as exc:
        raise CropError("裁切尺寸不是整数：{}".format(exc)) from exc
    if crop_w < 1 or crop_h < 1:
        raise CropError("裁切尺寸必须为正：{}x{}".format(crop_w, crop_h))
    if crop_w * crop_h > MAX_CROP_PIXELS:
        raise CropError(
            "裁切尺寸过大：{}x{} 超过上限 {} 像素".format(
                crop_w, crop_h, MAX_CROP_PIXELS
            )
        )
    return crop_w, crop_h


def _ensure_output_dir(output_dir: str) -> None:
    """Create the output directory and check that it is writable."""

    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise CropError(
            "输出目录不可写：{}（{}）".format(output_dir, exc)
        ) from exc
    if not os.path.isdir(output_dir):
        raise CropError("输出目录不可写：{}（不是目录）".format(output_dir))
    if not os.access(output_dir, os.W_OK):
        raise CropError("输出目录不可写：{}（没有写权限）".format(output_dir))


def _save_options(fmt: str) -> dict:
    """Return the encoder parameters of one output format.

    A JPEG is written at full quality without chroma subsampling, a
    WEBP losslessly and a TIFF with deflate compression, so a crop of
    a lossless source stays lossless.
    """

    if fmt == "JPEG":
        return {"quality": 100, "subsampling": 0}
    if fmt == "WEBP":
        return {"lossless": True}
    if fmt == "TIFF":
        return {"compression": "tiff_deflate"}
    return {}


def _resample(image, width, height):
    """Return a crop resampled to the exact size it is saved with.

    The filter is the module constant, so no call can change the
    result behind the back of the window. A mode the installed Pillow
    can not resample is reported as a CropError instead of a traceback
    inside the GUI slot.
    """

    try:
        return image.resize((width, height), RESAMPLE)
    except (OSError, ValueError) as exc:
        raise CropError(
            "缩放裁切结果失败：{}x{}（{}）".format(width, height, exc)
        ) from exc


def crop_image(
    source_path: str,
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    pad_x: int = 0,
    pad_y: int = 0,
    fill: int = FILL,
    output_dir: str = "",
    save_width: int = 0,
    save_height: int = 0,
) -> CropResult:
    """Crop one rectangle out of an image and save it as a new file.

    The crop is composed on a background of the fill value first, then
    the visible part of the source is pasted into its top left corner,
    then the four padding bands are painted. When the saved size
    differs from the crop size the composed image is resampled to
    exactly that size before it is encoded; an axis of zero follows
    the crop axis, so the default pair (0, 0) saves the composed crop
    untouched. The result is encoded to a .part file and moved over
    its final name, so a name is never half written and an existing
    file is never overwritten: a taken name only moves the sequence
    number one step further.

    The crop is written flat into output_dir, or into
    default_output_dir() when output_dir is empty, never into a
    subdirectory: the folder of the source is not mirrored. Two
    sources with the same stem therefore share one name sequence
    there, see scan_crops.

    Every condition that cannot be met - an unwritable output
    directory, an unreadable or too large image, a crop that is empty
    or too large or a name that exceeds MAX_NAME_BYTES - ends in a
    CropError and leaves the output directory without a new image.
    """

    target_dir = output_dir or default_output_dir()
    _ensure_output_dir(target_dir)
    source = _read_source(source_path)
    crop_w, crop_h = _crop_size(width, height)
    save_w, save_h = resolve_save_size(
        crop_w, crop_h, save_width, save_height
    )
    mode = _working_mode(source.mode)
    if mode != source.mode:
        source = source.convert(mode)
    left = max(0, int(x))
    top = max(0, int(y))
    image = PIL.Image.new(mode, (crop_w, crop_h), _fill_value(mode, fill))
    right = min(left + crop_w, source.width)
    bottom = min(top + crop_h, source.height)
    if right > left and bottom > top:
        # left/top are clamped to zero, so the visible part always
        # starts in the top left corner of the crop.
        image.paste(source.crop((left, top, right, bottom)), (0, 0))
    pad_left = _clamp_pad(pad_x, crop_w // 2)
    pad_top = _clamp_pad(pad_y, crop_h // 2)
    value = _fill_value(mode, fill)
    if pad_left > 0:
        band = PIL.Image.new(mode, (pad_left, crop_h), value)
        image.paste(band, (0, 0))
        image.paste(band, (crop_w - pad_left, 0))
    if pad_top > 0:
        band = PIL.Image.new(mode, (crop_w, pad_top), value)
        image.paste(band, (0, 0))
        image.paste(band, (0, crop_h - pad_top))
    if (save_w, save_h) != (crop_w, crop_h):
        image = _resample(image, save_w, save_h)
    out_ext, fmt, allowed = save_format(source_path)
    if allowed is not None and image.mode not in allowed:
        image = image.convert("RGB")
    options = _save_options(fmt)
    saved_mode = image.mode
    stem = os.path.splitext(os.path.basename(source_path))[0]
    for seq in range(1, MAX_NAME_ATTEMPTS + 1):
        name = build_crop_name(
            stem, left, top, crop_w, crop_h, pad_left, pad_top, out_ext, seq
        )
        length = len(name.encode("utf-8"))
        if length > MAX_NAME_BYTES:
            raise CropError(
                "文件名过长：{} 字节，超过上限 {} 字节（{}）".format(
                    length, MAX_NAME_BYTES, name
                )
            )
        final = os.path.join(target_dir, name)
        part = final + PART_SUFFIX
        if os.path.lexists(final):
            # The name is taken (by a crop or by a dangling link): only
            # the sequence number moves on, nothing is overwritten.
            continue
        try:
            with open(part, "xb") as handle:
                image.save(handle, fmt, **options)
        except FileExistsError:
            continue
        except (OSError, ValueError) as exc:
            raise CropError(
                "写入裁切文件失败：{}（{}）".format(part, exc)
            ) from exc
        try:
            os.replace(part, final)
        except OSError as exc:
            raise CropError(
                "写入裁切文件失败：{}（{}）".format(final, exc)
            ) from exc
        record = CropRecord(
            path=final,
            source_stem=stem,
            x=left,
            y=top,
            w=crop_w,
            h=crop_h,
            pad_x=pad_left,
            pad_y=pad_top,
            seq=seq,
            ext=out_ext.lstrip("."),
        )
        return CropResult(
            path=final,
            record=record,
            size=(save_w, save_h),
            mode=saved_mode,
        )
    raise CropError(
        "无法为 {} 生成唯一的裁切文件名（已尝试 {} 次）".format(
            stem, MAX_NAME_ATTEMPTS
        )
    )


def delete_crops(
    paths: Sequence[str], output_dir: str
) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Delete crop files of an output directory, white list only.

    Returns (deleted, skipped) with skipped a list of (path, reason)
    pairs. A path is only removed when the output directory exists,
    the base name carries the crop template, the path is a regular
    file and not a symbolic link, and its real path stays inside the
    output directory. A crop is written flat into the output root, but
    the whole tree is accepted as the white list rule, so a crop an
    older build left in a subdirectory can still be removed. Nothing
    is ever removed recursively and no directory is ever removed.
    """

    deleted: List[str] = []
    skipped: List[Tuple[str, str]] = []
    if not output_dir or not os.path.isdir(output_dir):
        for path in paths:
            skipped.append((path, "输出目录不可用"))
        return deleted, skipped
    root = os.path.realpath(output_dir)
    for path in paths:
        name = os.path.basename(path)
        if parse_crop_name(name) is None:
            skipped.append((path, "文件名不符合裁切模板"))
            continue
        if os.path.islink(path):
            skipped.append((path, "符号链接"))
            continue
        if not os.path.isfile(path):
            skipped.append((path, "不是普通文件"))
            continue
        if not _inside_output(path, root):
            skipped.append((path, "不在输出目录内"))
            continue
        try:
            os.remove(path)
        except OSError as exc:
            skipped.append((path, "删除失败：{}".format(exc)))
            continue
        deleted.append(path)
    return deleted, skipped

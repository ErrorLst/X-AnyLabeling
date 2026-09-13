"""Disk access, guards and plain geometry of the smudge tool.

The module is a Qt free helper layer around one image file: it reads an
image into an array, keeps the original safe in a backup directory
before the first write, writes an array back over the file and validates
the regions the user draws. Keeping it free of Qt makes the whole file
layer unit testable without a widget.

A JPEG is written back with the encoder parameters of the file it came
from - quantization tables, chroma subsampling, ICC profile and EXIF -
and a PNG with the ICC profile and the EXIF block it carried, so a
repair only pays for the pixels it changed instead of re-encoding the
whole picture at the Pillow defaults. Every parameter is gated on the
format it was read from, and a TIFF is not one of the formats of any
of them: it keeps the plain save. Its EXIF is the image file
directory of the file, whose own tags would override the ones the
writer sets, and its ICC profile is the encoder argument the writer
alone would turn back into an image file directory tag.

Only modes whose array round trips losslessly are accepted: an image is
written back with the same dtype and, for 16 bit grayscale, with the
little endian mode ``I;16``. Everything else - palette, ``F``, ``I``,
``CMYK`` - is rejected with a message instead of being converted, so the
tool can never damage a file it does not understand.
"""

import os
import os.path as osp
import shutil
import tempfile
import time

import numpy as np
import PIL.Image
from PIL import JpegImagePlugin

#: Image modes the tool can read and write back without a conversion.
SUPPORTED_MODES = ("L", "LA", "RGB", "RGBA", "I;16", "I;16L", "I;16B")

#: PIL mode used to write a 16 bit grayscale array back.
WRITE_16BIT_MODE = "I;16"

#: Largest accepted side of the region of interest, in pixels.
MAX_SIDE = 2000

#: Smallest accepted side of the region of interest, in pixels.
MIN_SIDE = 6

#: Directory holding every backup of this application.
BACKUP_ROOT = "dsh-smudge"

#: Encoder parameters of a file that survive a write back.
INFO_KEYS = ("qtables", "subsampling", "icc_profile", "exif")

#: Formats whose pixels are encoded by the JPEG encoder, so the
#: quantization tables and the chroma subsampling of the file can be
#: handed back to it. An MPO is a container of JPEG frames.
JPEG_FORMATS = ("JPEG", "MPO")

#: Formats whose EXIF block is a plain payload Pillow can take back as
#: is. A TIFF is left out on purpose: its ``getexif()`` returns the
#: whole image file directory, image width and length included, and the
#: writer merges those tags back after it has set its own, so a file
#: used to be written with tags that no longer matched its pixels.
EXIF_FORMATS = ("JPEG", "MPO", "PNG")

#: Formats whose ICC profile is a plain payload handed back as is.
#: The three formats of :data:`EXIF_FORMATS` carry a profile of that
#: kind. A TIFF is left out for another reason than the one above:
#: its profile has no tag to merge, it is only a payload as well, but
#: the writer would put it into an image file directory tag of its
#: own, so a file that carries one used to be written back with a tag
#: the old plain save never added. The gate keeps the promise that a
#: TIFF write passes no encoder argument at all.
ICC_FORMATS = ("JPEG", "MPO", "PNG")


class SmudgeError(Exception):
    """A failure the user has to be told about, in Chinese."""


def default_backup_dir():
    """Return a fresh backup directory inside the system temp folder.

    The name carries a timestamp and the process id, so two sessions
    never share a directory and a backup can always be traced back to the
    run that created it. The directory itself is created on demand.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "{}-{}".format(stamp, os.getpid())
    return osp.join(tempfile.gettempdir(), BACKUP_ROOT, name)


def read_image(path):
    """Read an image file into an array.

    Args:
        path: The image file to read.

    Returns:
        ``(array, image_format, original_mode, info)``. The array is a
        writable copy that owns its memory, so the file can be closed
        right away. ``image_format`` is the PIL format name used to write
        the file back, ``original_mode`` the PIL mode it was stored in,
        and ``info`` the encoder parameters of the file, to be handed
        back to :func:`write_image`. See :func:`image_info`.

    Raises:
        SmudgeError: The file cannot be read or holds a mode the tool
            does not support. The message names the mode.
    """
    try:
        with PIL.Image.open(path) as image:
            mode = image.mode
            image_format = image.format or _format_from_path(path)
            if mode not in SUPPORTED_MODES:
                raise SmudgeError(
                    "暂不支持 {} 模式的图像（模式：{}），无法涂抹修复".format(
                        image_format, mode
                    )
                )
            array = np.array(image)
            info = image_info(image)
    except SmudgeError:
        raise
    except Exception as error:
        raise SmudgeError("无法读取图像文件：{}".format(error))
    return array, image_format, mode, info


def empty_info():
    """Return an info mapping without any encoder parameter."""
    return {key: None for key in INFO_KEYS}


def image_info(image):
    """Return the encoder parameters of an opened image, best effort.

    The mapping has one key per entry of :data:`INFO_KEYS`; a value is
    ``None`` when the file does not carry that parameter or when it
    cannot be read. Reading the metadata of a file is an optimisation
    only: every single failure degrades to ``None`` and never disturbs
    the image itself.
    """
    info = empty_info()
    for key, getter in (
        ("qtables", _quantization_tables),
        ("subsampling", jpeg_subsampling),
        ("icc_profile", _icc_profile),
        ("exif", _exif_bytes),
    ):
        try:
            info[key] = getter(image)
        except Exception:  # noqa: BLE001
            info[key] = None
    return info


def _quantization_tables(image):
    """Return the quantization tables of an image, or ``None``.

    The tables are the ones the file was decoded with, so writing them
    back quantises the pixels the way the file already was: only the
    edited region pays a new generation of loss.
    """
    tables = getattr(image, "quantization", None)
    return dict(tables) if tables else None


def _icc_profile(image):
    """Return the embedded ICC profile of an image, or ``None``.

    Only a format that carries a plain one is read - see
    :data:`ICC_FORMATS`. A TIFF is skipped, so no profile of it ever
    reaches the writer as an argument.
    """
    if getattr(image, "format", None) not in ICC_FORMATS:
        return None
    profile = image.info.get("icc_profile")
    return bytes(profile) if profile else None


def _exif_bytes(image):
    """Return the EXIF block of an image, or ``None``.

    Only a format whose EXIF is a plain payload is read - see
    :data:`EXIF_FORMATS`. For a TIFF the block is the image file
    directory, so it is skipped before it is serialised, both to spare
    the work and to keep it away from the writer.

    An empty :class:`PIL.Image.Exif` still serialises to a bare TIFF
    header, so passing it would add an EXIF block to every file that
    never had one - a PNG would grow by a chunk for nothing. Only a
    block with at least one tag is kept.
    """
    if getattr(image, "format", None) not in EXIF_FORMATS:
        return None
    exif = image.getexif()
    if not len(exif):
        return None
    return exif.tobytes()


def jpeg_subsampling(image):
    """Return the chroma subsampling code of an opened JPEG, or ``None``.

    An MPO is a container of JPEG frames and answers like one. Pillow
    keeps the factor out of ``info``: the only source is the
    decoder state of an already opened file, and a handle or a path
    yields ``-1`` instead, so the image object itself is required. The
    codes are the ones :meth:`PIL.Image.Image.save` accepts, 0 for
    4:4:4, 1 for 4:2:2 and 2 for 4:2:0. Pillow answers ``-1`` for a
    grayscale image and for a CMYK one, where the factor means nothing,
    and for an image whose layer sampling it does not recognise. A
    grayscale file is the only one of those this tool reads, and its
    ``-1`` is written back as 4:4:4. Any other value - a file of any
    other format included - yields ``None``, so no argument is
    passed at all.
    """
    if getattr(image, "format", None) not in JPEG_FORMATS:
        return None
    try:
        factor = JpegImagePlugin.get_sampling(image)
    except Exception:  # noqa: BLE001
        return None
    if factor == -1:
        return 0
    if factor in (0, 1, 2):
        return int(factor)
    return None


def _format_from_path(path):
    """Return a PIL format name guessed from the file extension."""
    extension = osp.splitext(str(path))[1].lstrip(".").upper()
    if extension == "JPG":
        return "JPEG"
    if extension == "TIF":
        return "TIFF"
    return extension or "PNG"


def backup_original(path, backup_dir):
    """Copy an image into the backup directory, before it is modified.

    The file itself is never touched, only copied, and an existing backup
    is never overwritten: a name clash gets a numeric suffix. The caller
    remembers the returned path, so a file is backed up at most once per
    directory.

    Args:
        path: The image file to back up.
        backup_dir: The directory to copy it into, created when missing.

    Returns:
        The full path of the copy.

    Raises:
        SmudgeError: The copy failed.
    """
    try:
        os.makedirs(backup_dir, exist_ok=True)
    except OSError as error:
        raise SmudgeError("无法创建备份目录 {}：{}".format(backup_dir, error))
    target = osp.join(backup_dir, osp.basename(path))
    index = 1
    while osp.exists(target):
        stem, extension = osp.splitext(osp.basename(path))
        target = osp.join(backup_dir, "{}-{}{}".format(stem, index, extension))
        index += 1
    try:
        shutil.copyfile(path, target)
    except OSError as error:
        raise SmudgeError("备份原图失败：{}".format(error))
    return target


def write_image(array, path, image_format, info=None):
    """Write an array back over an image file.

    The file is replaced in place, with the format it already had, so
    the original is never left behind under a different name. A 16 bit
    grayscale array is written with the little endian mode ``I;16``,
    which is what Pillow stores for both ``I;16`` and ``I;16L`` input.

    Args:
        array: The image data, ``uint8`` or ``uint16``.
        path: The file to overwrite.
        image_format: The PIL format name to save with, for example the
            value returned by :func:`read_image`.
        info: The encoder parameters of the file, as returned by
            :func:`read_image`. They are handed to Pillow, so a JPEG is
            quantised again exactly as it already was and only the edited
            pixels change; ``None`` saves with the Pillow defaults, the
            behaviour the tool had before. Every parameter is gated on
            the format it belongs to - see :data:`JPEG_FORMATS`,
            :data:`ICC_FORMATS` and :data:`EXIF_FORMATS` - so a TIFF
            keeps the plain save.

    Raises:
        SmudgeError: The write failed; the caller has to fall back to the
            state before the operation.
    """
    try:
        image = _to_pil(array)
    except (TypeError, ValueError) as error:
        raise SmudgeError("无法写出该图像数据：{}".format(error))
    try:
        _save_image(image, path, image_format, info)
    except Exception as error:
        raise SmudgeError("写入图像文件失败：{}".format(error))
    finally:
        image.close()


def _save_image(image, path, image_format, info):
    """Save an image with the encoder arguments carried by ``info``.

    A writer that refuses one of the arguments must never cost the user
    the whole image: the plain save then writes every pixel, with the
    Pillow defaults, the way this module did before the arguments were
    collected. The fall back is not reported back to the caller, so the
    ``info`` mapping of a file written this way keeps describing the
    parameters the file was read with rather than the ones it now
    carries: the next write tries them again, which costs one refused
    save attempt and never a pixel.
    """
    resolved = image_format or _format_from_path(path)
    options = _save_options(resolved, info)
    if options:
        try:
            image.save(path, format=resolved, **options)
            return
        except Exception:  # noqa: BLE001
            pass
    image.save(path, format=resolved)


def _save_options(image_format, info):
    """Return the Pillow save arguments held in an info mapping.

    Nothing is passed that the file did not carry, so an empty mapping
    yields no argument at all and the plain save path stays untouched.
    The chroma subsampling only means something for a JPEG, where Pillow
    also refuses ``None``, so it is passed for a JPEG or an MPO with a
    real code only. The EXIF block is passed for the formats that carry
    a plain one - see :data:`EXIF_FORMATS` - and never for a TIFF,
    whose image file directory would override the tags the writer sets
    for the file being written. The ICC profile follows the same
    rule as the EXIF block, for the formats of :data:`ICC_FORMATS`,
    so a TIFF also never carries a profile tag the old save did not
    write.
    """
    if not info:
        return {}
    options = {}
    qtables = info.get("qtables")
    if qtables:
        options["qtables"] = qtables
    subsampling = info.get("subsampling")
    if image_format in JPEG_FORMATS and isinstance(subsampling, int):
        options["subsampling"] = subsampling
    icc_profile = info.get("icc_profile")
    if image_format in ICC_FORMATS and icc_profile:
        options["icc_profile"] = icc_profile
    exif = info.get("exif")
    if image_format in EXIF_FORMATS and exif:
        options["exif"] = exif
    return options


def _to_pil(array):
    """Return a PIL image for a two or three dimensional array."""
    data = np.ascontiguousarray(array)
    if data.dtype.kind == "u" and data.dtype.itemsize == 2:
        # Pillow hands a big endian 16 bit file over as ">u2", which is
        # the same image with another byte order: normalise it, so a
        # 16 bit file that can be read can be written back as well.
        data = data.astype(np.uint16)
    if data.dtype == np.uint16:
        if data.ndim != 2:
            raise TypeError("16 位图像只支持单通道")
        return PIL.Image.fromarray(data, WRITE_16BIT_MODE)
    if data.dtype != np.uint8:
        raise TypeError(
            "只支持 8 位和 16 位图像，当前为 {}".format(data.dtype)
        )
    if data.ndim == 2:
        return PIL.Image.fromarray(data, "L")
    if data.shape[2] == 3:
        return PIL.Image.fromarray(data, "RGB")
    if data.shape[2] == 4:
        return PIL.Image.fromarray(data, "RGBA")
    if data.shape[2] == 2:
        return PIL.Image.fromarray(data, "LA")
    raise TypeError("不支持的通道数：{}".format(data.shape[2]))


def validate_roi(roi, shape):
    """Check a region of interest against the size of its image.

    Args:
        roi: ``(x0, y0, x1, y1)``, half open.
        shape: The shape of the image the region belongs to.

    Raises:
        SmudgeError: The region is empty, too small, too large or outside
            the image. The message is meant for the user.
    """
    x0, y0, x1, y1 = (int(value) for value in roi)
    if x1 <= x0 or y1 <= y0:
        raise SmudgeError("矩形太小（至少 6 像素），请重新框选")
    if too_small((x0, y0, x1, y1)):
        raise SmudgeError("矩形太小（至少 6 像素），请重新框选")
    if (x1 - x0) > MAX_SIDE or (y1 - y0) > MAX_SIDE:
        raise SmudgeError("矩形过大（单边上限 2000 像素）")
    height, width = shape[:2]
    if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
        raise SmudgeError("矩形超出图像范围，请重新框选")


def source_window(center, size, shape):
    """Return the source window centred on a point of the image.

    The window has the size of the region of interest and is clamped into
    the image, so a source point near a border still yields a window the
    matching can use: it is shifted inwards instead of being cut short.
    A window that cannot reach the requested size, on an image smaller
    than the region, is returned as large as the image allows.

    Args:
        center: ``(cx, cy)`` of the source point, in image coordinates.
        size: ``(width, height)`` of the region of interest.
        shape: The shape of the image.

    Returns:
        ``(x0, y0, x1, y1)``, half open, inside the image.
    """
    height, width = shape[:2]
    window_w = min(max(1, int(round(size[0]))), width)
    window_h = min(max(1, int(round(size[1]))), height)
    x0 = int(round(center[0] - window_w / 2))
    y0 = int(round(center[1] - window_h / 2))
    x0 = int(np.clip(x0, 0, width - window_w))
    y0 = int(np.clip(y0, 0, height - window_h))
    return (x0, y0, x0 + window_w, y0 + window_h)


def roi_box(press, release):
    """Return the region dragged between two image points.

    The two corners are normalised and rounded to whole pixels, so a drag
    in any direction yields the same half open box.

    Args:
        press: ``(x, y)`` where the drag started.
        release: ``(x, y)`` where the drag ended.

    Returns:
        ``(x0, y0, x1, y1)``, half open, with ``x1 > x0`` and
        ``y1 > y0`` whenever the drag moved at all.
    """
    x0, x1 = sorted((int(round(press[0])), int(round(release[0]))))
    y0, y1 = sorted((int(round(press[1])), int(round(release[1]))))
    return (x0, y0, x1, y1)


def too_small(box):
    """Return ``True`` when a side of the box is below six pixels."""
    x0, y0, x1, y1 = box
    return (x1 - x0) < MIN_SIDE or (y1 - y0) < MIN_SIDE

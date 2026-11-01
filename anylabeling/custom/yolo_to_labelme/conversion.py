"""Convert a ready YOLO plan into LabelMe files through the upstream code.

The converter object and the label format are owned by
anylabeling.views.labeling.label_converter.LabelConverter: this module
only dispatches one pair at a time, counts the outcomes and keeps a
failure list. The heavy import happens inside convert_pairs, so importing
this module during application start-up stays cheap.
"""

from __future__ import annotations

import os.path as osp
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from .yolo_core import MODE_OBB, MODE_POLYGON, Pair

__all__ = ["ConversionResult", "convert_pairs"]


@dataclass
class ConversionResult:
    """Outcome counters of one conversion run."""

    converted: int = 0
    empty: int = 0
    skipped_existing: int = 0
    failed: List[Tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False

    @property
    def attempted(self) -> int:
        return (
            self.converted
            + self.skipped_existing
            + len(self.failed)
        )


def _build_converter(class_names: Sequence[str]):
    """Create the upstream converter with the class table installed."""

    from anylabeling.views.labeling.label_converter import LabelConverter

    converter = LabelConverter()
    converter.classes = list(class_names)
    return converter


def _convert_one(converter, pair: Pair) -> None:
    """Write the LabelMe file of one pair, mode by mode."""

    if pair.mode == MODE_OBB:
        converter.yolo_obb_to_custom(
            input_file=pair.label_path,
            output_file=pair.output_path,
            image_file=pair.image_path,
        )
        return
    upstream_mode = "seg" if pair.mode == MODE_POLYGON else "hbb"
    converter.yolo_to_custom(
        input_file=pair.label_path,
        output_file=pair.output_path,
        image_file=pair.image_path,
        mode=upstream_mode,
    )


def convert_pairs(
    pairs: Sequence[Pair],
    class_names: Sequence[str],
    overwrite: bool = False,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> ConversionResult:
    """Convert every pair, one file at a time.

    An existing json is skipped unless overwrite is true. A failure of one
    pair is recorded and the run continues. The output directory is the
    image directory, which already exists; no source file is touched and
    nothing is deleted.
    """

    result = ConversionResult()
    if not pairs:
        return result
    converter = _build_converter(class_names)
    total = len(pairs)
    for index, pair in enumerate(pairs, 1):
        if should_cancel is not None and should_cancel():
            result.cancelled = True
            break
        try:
            if osp.isfile(pair.output_path) and not overwrite:
                result.skipped_existing += 1
            else:
                _convert_one(converter, pair)
                result.converted += 1
                if not pair.mode:
                    result.empty += 1
        except Exception as error:  # noqa: BLE001
            result.failed.append((pair.image_path, str(error)))
        if on_progress is not None:
            on_progress(index, total, pair.image_path)
    return result

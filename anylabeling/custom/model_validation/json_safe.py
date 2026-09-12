"""JSON safety helpers shared by the report and the export layers.

A validation report is assembled from values coming from many sources:
the configuration page, the ONNX metadata, the augmentation stage and
the UI itself. A single foreign value - a Qt object, a numpy scalar, a
Path, or a callback captured by mistake - used to make the whole export
unusable, because json.dumps refuses to serialise it and the archive was
never written.

Two independent lines of defence keep the delivery alive:

* build_report runs every section through sanitize(), which rebuilds the
  document with the JSON primitives only (dict, list, str, int, float,
  bool, None) and degrades anything else to a readable text;
* the serialising helpers below - json_default, dumps_safe and
  dumps_safe_bytes - keep the last resort of the encoder, so even a
  document that never went through sanitize() is written instead of
  raising in the middle of a delivery. They are the compatible API of
  the previous revision: the export of this revision serialises no
  document at all, because write_zip copies the staged xlabel json into
  the archive byte for byte (see exporter.write_zip).

Nothing here raises for an unexpected value: the purpose of both layers
is that a foreign object can never break an export.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Mapping

# A report is a tree of a few levels; the guard only exists so that a
# self referencing structure degrades into text instead of exhausting
# the recursion limit of the interpreter.
MAX_NESTING = 64


def describe(value: Any) -> str:
    """Return a readable text for a value JSON cannot hold.

    Callables keep their name (the reported value tells which callback
    leaked into the document), every other object falls back to its own
    text, and the default repr carrying an address is replaced by the
    type name so that the written report stays comparable from one run
    to the next.
    """

    name = getattr(value, "__name__", None) or getattr(
        value, "__qualname__", None
    )
    if callable(value) and name:
        return f"<callable {name}>"
    text = ""
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - a broken __str__ must not fail
        text = ""
    if not text or (text.startswith("<") and " at 0x" in text):
        text = f"<{type(value).__name__}>"
    return text


def _safe_key(key: Any) -> Any:
    """Return a JSON friendly dictionary key."""

    if isinstance(key, str) or key is None:
        return key
    if isinstance(key, bool) or isinstance(key, (int, float)):
        return key
    return describe(key)


def _foreign(value: Any):
    """Return (handled, converted) for the well known foreign types.

    Only duck typing is used: numpy is never imported here, so a report
    can be written without paying for the numeric stack.
    """

    ndim = getattr(value, "ndim", None)
    if ndim is not None:
        item = getattr(value, "item", None)
        if ndim == 0 and callable(item):
            return True, item()
        tolist = getattr(value, "tolist", None)
        if callable(tolist):
            return True, tolist()
    if isinstance(value, (bytes, bytearray)):
        return True, bytes(value).decode("utf-8", "replace")
    return False, None


def _sanitize(value: Any, depth: int) -> Any:
    if depth > MAX_NESTING:
        return describe(value)
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    handled, converted = _foreign(value)
    if handled:
        return _sanitize(converted, depth + 1)
    if isinstance(value, Mapping):
        return {
            _safe_key(key): _sanitize(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        # a set has no order of its own: sorting by text keeps the
        # written report identical from one run to the next
        return [
            _sanitize(item, depth + 1)
            for item in sorted(value, key=lambda item: str(item))
        ]
    if is_dataclass(value) and not isinstance(value, type):
        return _sanitize(asdict(value), depth + 1)
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _sanitize(to_dict(), depth + 1)
        except Exception:  # noqa: BLE001 - degrade instead of failing
            return describe(value)
    return describe(value)


def sanitize(value: Any) -> Any:
    """Return a JSON serialisable copy of value.

    The copy holds dictionaries, lists, strings, integers, floats,
    booleans and None only. Paths and paths like objects become strings,
    numpy scalars and arrays become Python scalars and lists, dataclasses
    become dictionaries and every other object - a callback, a Qt
    object, a generator - degrades to a readable text.
    """

    return _sanitize(value, 0)


def json_default(value: Any) -> str:
    """Return the text stored in place of a value json refuses to write."""

    return describe(value)


def dumps_safe(value: Any, **kwargs: Any) -> str:
    """Serialise value, degrading anything JSON cannot hold to text.

    The document is sanitised first and the encoder keeps a last resort
    handler, therefore this function never raises the TypeError of
    json.dumps for an unexpected object.
    """

    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("default", json_default)
    return json.dumps(sanitize(value), **kwargs)


def dumps_safe_bytes(value: Any, **kwargs: Any) -> bytes:
    """Return the UTF-8 bytes of dumps_safe."""

    return dumps_safe(value, **kwargs).encode("utf-8")


def as_mapping(value: Any) -> Dict[str, Any]:
    """Return value as a JSON friendly dictionary.

    A value that is not a mapping degrades to an empty dictionary: the
    report keeps its shape and the export keeps working when a caller
    hands over something else than a snapshot.
    """

    sanitized = sanitize(value)
    if isinstance(sanitized, dict):
        return sanitized
    return {}


def as_list(value: Any) -> List[Any]:
    """Return value as a JSON friendly list.

    A list like value is copied, None and a mapping become an empty
    list, and a single scalar - including a lone string - becomes a one
    item list instead of being split into characters.
    """

    sanitized = sanitize(value)
    if isinstance(sanitized, list):
        return sanitized
    if sanitized is None or isinstance(sanitized, dict):
        return []
    return [sanitized]


__all__ = [
    "MAX_NESTING",
    "as_list",
    "as_mapping",
    "describe",
    "dumps_safe",
    "dumps_safe_bytes",
    "json_default",
    "sanitize",
]

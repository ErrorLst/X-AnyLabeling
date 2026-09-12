"""Import level checks for the model validation package."""

import io
import os
import os.path as osp
import tokenize

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import importlib

PACKAGE_DIR = osp.dirname(
    osp.abspath(
        importlib.import_module("anylabeling.custom.model_validation").__file__
    )
)
TESTS_DIR = osp.dirname(osp.abspath(__file__))


def test_package_import_exposes_launcher():
    module = importlib.import_module("anylabeling.custom.model_validation")
    assert hasattr(module, "launch_model_validation")
    assert module.__all__ == ["launch_model_validation"]


def test_ui_dialog_imports():
    module = importlib.import_module(
        "anylabeling.custom.model_validation.ui.dialog"
    )
    assert hasattr(module, "ModelValidationDialog")
    # the height follows the content now: no hard coded 1024 x 680 that
    # left an empty area below the compact configuration page.
    assert module.MINIMUM_WIDTH == 1024
    assert module.MINIMUM_HEIGHT == 600
    assert module.WINDOW_SIZE == (
        module.MINIMUM_WIDTH,
        module.MINIMUM_HEIGHT,
    )


def test_core_modules_import():
    names = [
        "app_config",
        "dataset",
        "labelme_io",
        "onnx_meta",
        "records",
        "report",
        "judge",
        "exporter",
        "inference",
        "pipeline",
    ]
    for name in names:
        module = importlib.import_module(
            f"anylabeling.custom.model_validation.{name}"
        )
        assert module is not None


def test_launcher_does_not_import_albumentations_eagerly():
    import anylabeling.custom.model_validation.launcher as launcher

    assert callable(launcher.launch_model_validation)


def literal_quote(text: str) -> str:
    """Return the quote run a python string token starts with."""

    for index, char in enumerate(text):
        if char not in "'" + chr(34):
            continue
        run = text[index:]
        if run.startswith(char * 3):
            return char * 3
        if run.startswith(char * 2):
            return char * 2
        return char
    return ""


def quoting_conflict(open_quote: str, nested_quote: str) -> bool:
    """Return True when a nested literal ends the enclosing f-string.

    A single quoted f-string is terminated by its own quote character,
    a triple quoted one only by the very same triple, therefore the
    widespread triple quoted style sheet f-strings stay valid on 3.11.
    """

    if not open_quote or not nested_quote:
        return False
    if len(open_quote) == 1:
        return nested_quote[0] == open_quote[0]
    return nested_quote == open_quote


def pep701_offenders(source: str):
    """Return the f-strings that only Python 3.12 and later accept.

    Before PEP 701 an f-string could not reuse its own quote character
    inside a replacement field, neither for a nested string literal nor
    for a nested f-string. The project declares requires-python >= 3.11,
    therefore such a literal breaks the window on 3.11.
    """

    start_token = getattr(tokenize, "FSTRING_START", None)
    if start_token is None:  # Python 3.11 tokenises the whole literal
        return []
    offenders = []
    stack = []
    for item in tokenize.generate_tokens(io.StringIO(source).readline):
        if item.type == start_token:
            quote = literal_quote(item.string)
            if stack and quoting_conflict(stack[-1], quote):
                offenders.append((item.start[0], item.string))
            stack.append(quote)
        elif item.type == tokenize.FSTRING_END:
            if stack:
                stack.pop()
        elif item.type == tokenize.STRING and stack:
            quote = literal_quote(item.string)
            if quoting_conflict(stack[-1], quote):
                offenders.append((item.start[0], item.string))
    return offenders


def scan_tree(root: str):
    """Return every 3.12 only f-string of a python source tree."""

    offenders = []
    for current, _dirs, names in os.walk(root):
        if "__pycache__" in current:
            continue
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            path = osp.join(current, name)
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            for line, literal in pep701_offenders(text):
                offenders.append(f"{path}:{line}: {literal}")
    return offenders


def test_the_fstring_scanner_detects_the_broken_quoting():
    quote = chr(34)
    broken = (
        "x = f" + quote + "a {d.get(" + quote + "k" + quote + ", 0)}" + quote
    )
    assert pep701_offenders(broken) == [(1, '"k"')]
    nested = "x = f" + quote + "{f" + quote + "{y}" + quote + "}" + quote
    assert pep701_offenders(nested)
    fine = "x = f" + quote + "a {d.get('k', 0)}" + quote
    assert pep701_offenders(fine) == []
    triple = (
        "x = f"
        + quote * 3
        + "a {d["
        + quote
        + "k"
        + quote
        + "]} b"
        + quote * 3
    )
    assert pep701_offenders(triple) == []


def test_package_and_tests_are_python_311_compatible():
    """No module may use the PEP 701 same quote f-string nesting."""

    offenders = scan_tree(PACKAGE_DIR) + scan_tree(TESTS_DIR)
    assert offenders == []

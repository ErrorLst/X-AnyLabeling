"""Debug entry point: python -m anylabeling.custom.model_validation"""

from __future__ import annotations

import sys


def main() -> int:
    """Open the model validation window in a standalone Qt application."""

    from PyQt6 import QtWidgets

    from .launcher import launch_model_validation

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    window = launch_model_validation(None)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

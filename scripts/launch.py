"""Windowed entrypoint with a visible, logged startup failure."""

import logging
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
(root / "data").mkdir(exist_ok=True)
logging.basicConfig(filename=root / "data" / "application.log", encoding="utf-8", level=logging.INFO)

try:
    from codexdoc.gui import run

    exit_code = run()
except Exception as exc:
    logging.exception("Application startup failed")
    from PySide6.QtWidgets import QApplication, QMessageBox

    application = QApplication.instance() or QApplication([])
    QMessageBox.critical(
        None,
        "CodexDoc",
        f"Не удалось открыть приложение: {exc}\n\nПодробности: {root / 'data' / 'application.log'}\nДля восстановления окружения запустите Настроить.cmd.",
    )
    exit_code = 1

raise SystemExit(exit_code)

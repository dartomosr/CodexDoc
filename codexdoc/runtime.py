"""Only the application-managed, version-locked office renderer is allowed."""

import os
import subprocess
from pathlib import Path

from codexdoc.storage import ROOT, digest, read_json


def renderer_info() -> dict:
    lock = read_json(ROOT / "runtime.lock.json")["libreoffice"]
    root = ROOT / ".runtime" / "libreoffice"
    marker = root / "runtime.json"
    if not marker.exists():
        raise ValueError("Рендерер не установлен. Запустите Настроить.cmd в папке CodexDoc.")
    state = read_json(marker)
    if state["sha256"] != lock["sha256"] or state["version"] != lock["version"]:
        raise ValueError("Версия рендерера отличается от runtime.lock.json. Запустите Настроить.cmd.")
    executable = (root / state["executable"]).resolve()
    if not executable.is_relative_to(root.resolve()) or not executable.is_file():
        raise ValueError("Повреждена локальная установка рендерера.")
    return {
        "version": state["version"],
        "executable": str(executable),
        "executable_hash": digest(executable),
        "installer_sha256": state["sha256"],
    }


def font_inventory(font_name: str) -> dict:
    # Exact font binaries are part of the build fingerprint, so changed fonts invalidate the cache.
    font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if font_name == "Times New Roman":
        files = [font_dir / name for name in ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf")]
        if not all(p.is_file() for p in files):
            raise ValueError(
                "Для выбранного шаблона отсутствует Times New Roman. Установите лицензированный шрифт или выберите доступный шрифт в шаблоне."
            )
    else:
        # Qt queries actual installed families, rather than guessing filenames.
        from PySide6.QtGui import QFontDatabase, QGuiApplication

        app = QGuiApplication.instance() or QGuiApplication([])
        if font_name not in QFontDatabase.families():
            raise ValueError(f"Шрифт «{font_name}» не установлен. Выберите существующий шрифт.")
        del app
        files = sorted(font_dir.glob("*.ttf")) + sorted(font_dir.glob("*.otf"))
    return {"family": font_name, "files": {str(p): digest(p) for p in files}}


def render_docx(source: Path, docx_output: Path, pdf_output: Path, timeout: int = 120) -> dict:
    info = renderer_info()
    program = Path(info["executable"]).parent
    python_home = next(program.glob("python-core-*"))
    env = os.environ.copy()
    env["PYTHONHOME"] = str(python_home)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(program), str(python_home / "lib"), str(python_home / "lib" / "site-packages")]
    )
    env["PYTHONUTF8"] = "1"
    command = [
        str(program / "python.exe"),
        str(ROOT / "scripts" / "lo_render.py"),
        str(source.resolve()),
        str(docx_output.resolve()),
        str(pdf_output.resolve()),
        info["executable"],
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        process.communicate()
        raise ValueError("Рендерер превысил время ожидания. Результат не считается готовым.") from exc
    if process.returncode or not pdf_output.is_file() or not docx_output.is_file():
        raise ValueError(f"Не удалось отобразить DOCX: {(stderr or stdout)[-2000:]}")
    return {**info, "log": stdout[-2000:], "stderr": stderr[-2000:]}

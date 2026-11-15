"""Read-only lifecycle assessment and conservative local title-field extraction."""

import re
from pathlib import Path

from codexdoc.checks import validate_inputs
from codexdoc.content_review import status as content_review_status
from codexdoc.models import CheckReport
from codexdoc.pipeline import fingerprint
from codexdoc.storage import contained, digest, read_json
from codexdoc.workspaces import load_workspace


def current_build(path: Path) -> tuple[CheckReport | None, str]:
    """Never promote a saved report to current without checking its inputs and outputs."""
    report_path = path / "output/checks.json"
    if not report_path.is_file():
        return None, "Документ ещё не собран"
    try:
        report = CheckReport.model_validate(read_json(report_path))
        manifest, template, _, content = load_workspace(path)
        if not {"document.docx", "document.pdf"}.issubset(report.artifact_hashes):
            return report, "Нет проверенной сборки"
        key, _ = fingerprint(path, template)
        if key != report.fingerprint:
            return report, "Содержание или настройки изменились. Требуется пересборка"
        for relative, expected in report.artifact_hashes.items():
            artifact = contained(path / "output", relative)
            if not artifact.is_file() or digest(artifact) != expected:
                return report, "Итоговые файлы изменены или отсутствуют. Требуется пересборка"
        # Sources and provenance have independent integrity checks in the engine.
        if any(
            f.code in {"source.changed", "source.path", "norm.changed", "norm.path"}
            for f in validate_inputs(path, manifest, template, content)
        ):
            return report, "Материалы изменились. Требуется повторная проверка"
        if report.status == "ready":
            semantic = content_review_status(path)
            if semantic["required"] and semantic["status"] != "passed":
                return report, "Содержательная проверка требует обновления"
            state = read_json(path / ".cache/build.json")
            reviews_path = path / ".cache/reviews.json"
            reviews = read_json(reviews_path) if reviews_path.is_file() else {}
            hashes = state.get("pdf", {}).get("page_hashes", {})
            if any(
                reviews.get(str(page), {}).get("page_hash") != hashes.get(str(page))
                or str(page) not in reviews
                for page in report.review_pages
            ):
                return report, "Просмотр страниц требует подтверждения"
        return report, ""
    except (OSError, ValueError, KeyError) as exc:
        return None, f"Не удалось подтвердить актуальность: {exc}"


def title_fields(text: str) -> dict[str, str]:
    """Accept only explicit labels; ambiguous repetitions are left for human/agent review."""
    labels = {
        "организация": "organization",
        "учебное заведение": "organization",
        "кафедра": "department",
        "подразделение": "department",
        "автор": "author",
        "выполнил": "author",
        "выполнила": "author",
        "группа": "group",
        "руководитель": "supervisor",
        "проверил": "supervisor",
        "проверила": "supervisor",
        "город": "city",
        "год": "year",
    }
    candidates: dict[str, set[str]] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\s*([^:|]{2,40})\s*[:|]\s*(\S.*?)\s*", line)
        if not match:
            continue
        key = labels.get(match[1].strip().casefold())
        value = match[2].strip()
        if key and len(value) <= 250 and (key != "year" or re.fullmatch(r"\d{4}", value)):
            candidates.setdefault(key, set()).add(value)
    return {key: next(iter(values)) for key, values in candidates.items() if len(values) == 1}

"""Build transactions, cache integrity, current-version review and publication."""

import importlib.metadata
import shutil
import tempfile
from pathlib import Path

from codexdoc import __version__
from codexdoc.checks import check_docx, inspect_pdf, validate_inputs
from codexdoc.content_review import status as content_review_status
from codexdoc.docx_engine import compose
from codexdoc.library import Library
from codexdoc.models import CheckReport, DocumentContent, Finding, Template
from codexdoc.previews import page_overview
from codexdoc.runtime import font_inventory, render_docx, renderer_info
from codexdoc.storage import (
    ROOT,
    atomic_text,
    contained,
    digest,
    now,
    object_digest,
    read_json,
    workspace_lock,
    write_json,
)
from codexdoc.workspaces import load_workspace


def fingerprint(workspace: Path, template: Template) -> tuple[str, dict]:
    files = {
        name: digest(workspace / name)
        for name in ("workspace.json", "template.json", "profile.json", "document.json")
    }
    content = DocumentContent.model_validate(read_json(workspace / "document.json"))
    for block in content.blocks:
        if block.type == "image":
            files[block.path] = digest(contained(workspace, block.path))
    fonts = {template.text.font}
    styles = []
    if template.title_layout:
        styles.extend([b.style for b in template.title_layout.blocks])
        styles.append(template.title_layout.bottom_style)
    for section in (template.contents_format, template.bibliography_format):
        if section:
            styles.extend([section.heading, section.entries])
    if template.structural_heading_style:
        styles.append(template.structural_heading_style)
    fonts.update(style.font for style in styles if style.font)
    environment = {
        "engine": __version__,
        "code": {p.name: digest(p) for p in sorted((ROOT / "codexdoc").glob("*.py"))},
        "render_script": digest(ROOT / "scripts" / "lo_render.py"),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("python-docx", "pydantic", "PyMuPDF", "Pillow")
        },
        "renderer": renderer_info(),
        "fonts": {font: font_inventory(font) for font in sorted(fonts)},
    }
    return object_digest({"files": files, "environment": environment}), environment


def save_report(workspace: Path, report: CheckReport):
    write_json(workspace / "output" / "checks.json", report)
    labels = {
        "ready": "Готово по выбранному профилю проверок",
        "review": "Нужна содержательная проверка и/или визуальный просмотр",
        "blocked": "Не готово: обнаружены ошибки",
        "preview": "Предпросмотр черновика: не подтверждён и не готов к публикации",
    }
    text = [
        labels[report.status],
        f"Страниц: {report.page_count}",
        f"Проверено: {report.checked_at}",
        f"Технические проверки: {report.technical_status}",
        f"Содержательная проверка: {report.content_review.get('status', 'not_checked')}",
        "Автоматически проверено: " + ", ".join(report.automatic_checks),
        "Нужен просмотр страниц: "
        + (", ".join(str(p) for p in report.review_pages if p not in report.reviewed_pages) or "нет"),
    ]
    for item in report.findings:
        location = f" [блок {item.block_id}]" if item.block_id else ""
        location += f" [стр. {item.page}]" if item.page else ""
        text.append(f"{item.severity}: {item.message}{location} ({item.code})")
    for stage, value in report.content_review.get("stages", {}).items():
        text.append(f"Содержание / {stage}: {value['status']}")
        assessment = value.get("assessment", {})
        if assessment:
            text.append(assessment["summary"])
        for entry in assessment.get("coverage", []):
            if entry["status"] != "covered":
                text.append(f"{entry['item_id']}: {entry['status']}: {entry['note']}")
        for finding in assessment.get("findings", []):
            text.append(
                f"{finding['severity']}: {finding['message']} [блок {finding['block_id']}] ({finding['code']})"
            )
    text.append(
        "Проверки не удостоверяют достоверность содержания и полное соответствие всем положениям ГОСТ. Содержательную проверку выполняет агент по заданию и нормативным материалам."
    )
    atomic_text(workspace / "output" / "checks.txt", "\n".join(text) + "\n")


def _blocked(workspace: Path, findings: list[Finding], key: str = "") -> CheckReport:
    report = CheckReport(
        findings=findings,
        fingerprint=key,
        checked_at=now(),
        technical_status="blocked",
        content_review=content_review_status(workspace),
    )
    save_report(workspace, report)
    return report


def build(workspace: Path, force: bool = False, preview: bool = False) -> CheckReport:
    workspace = workspace.resolve()
    with workspace_lock(workspace):
        return _build(workspace, force, preview)


def _build(workspace: Path, force: bool, preview: bool = False) -> CheckReport:
    manifest, template, profile, content = load_workspace(workspace)
    findings = validate_inputs(workspace, manifest, template, content, allow_template_draft=preview)
    if any(f.severity == "error" for f in findings):
        return _blocked(workspace, findings)
    if preview:
        findings.append(
            Finding(
                code="build.preview",
                message="Это визуальный предпросмотр черновика. Он не подтверждает шаблон и не разрешает публикацию.",
                severity="info",
            )
        )
    try:
        key, environment = fingerprint(workspace, template)
    except (ValueError, OSError) as exc:
        return _blocked(workspace, [Finding(code="runtime.unavailable", message=str(exc))])
    output = workspace / "output"
    state_path = workspace / ".cache" / "build.json"
    previous = read_json(state_path) if state_path.exists() else {}
    if (
        not force
        and not preview
        and previous.get("fingerprint") == key
        and not previous.get("preview", False)
        and _artifacts_match(output, previous.get("artifact_hashes", {}))
    ):
        return _check(workspace)
    state = compose(workspace, content, template, manifest.sources)
    staging = Path(tempfile.mkdtemp(prefix="render-", dir=workspace / ".cache"))
    try:
        render_meta = render_docx(
            workspace / ".cache" / "authored.docx", staging / "document.docx", staging / "document.pdf"
        )
        findings.extend(check_docx(staging / "document.docx", template, content))
        pdf_state = inspect_pdf(staging / "document.pdf", content, template, staging / "pages")
        findings.extend(pdf_state.pop("findings"))
        output.mkdir(exist_ok=True)
        # Keep the previous build for recovery, including its previous readiness report.
        if (output / "document.docx").exists():
            archive = workspace / ".cache" / "previous"
            if archive.exists():
                shutil.rmtree(archive)
            shutil.copytree(output, archive)
        for name in ("document.docx", "document.pdf"):
            (staging / name).replace(output / name)
        if (output / "pages").exists():
            shutil.rmtree(output / "pages")
        shutil.move(str(staging / "pages"), output / "pages")
        page_overview(workspace)
        hashes = {name: digest(output / name) for name in ("document.docx", "document.pdf")}
        write_json(
            state_path,
            {
                "fingerprint": key,
                "artifact_hashes": hashes,
                "preview": preview,
                "environment": environment,
                "composition": state,
                "render": render_meta,
                "pdf": pdf_state,
                "findings": [f.model_dump() for f in findings],
            },
        )
        return _report(workspace, key, hashes, findings, pdf_state, profile.verification, preview=preview)
    except (ValueError, OSError, RuntimeError) as exc:
        return _blocked(workspace, [Finding(code="render.failed", message=str(exc))], key)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _artifacts_match(output: Path, hashes: dict) -> bool:
    return bool(hashes) and all(
        (output / name).is_file() and digest(output / name) == sha for name, sha in hashes.items()
    )


def _report(
    workspace: Path,
    key: str,
    hashes: dict,
    findings: list[Finding],
    pdf_state: dict,
    verification: str,
    preview: bool = False,
) -> CheckReport:
    candidates = pdf_state["review_pages"]
    if verification == "strict":
        candidates = list(range(1, pdf_state["page_count"] + 1))
    elif verification == "quick":
        candidates = sorted({1, *(f.page for f in findings if f.page)})
    reviews_path = workspace / ".cache" / "reviews.json"
    reviews = read_json(reviews_path) if reviews_path.exists() else {}
    reviewed = [
        number
        for number in candidates
        if reviews.get(str(number), {}).get("page_hash") == pdf_state["page_hashes"].get(str(number))
    ]
    semantic = content_review_status(workspace)
    if semantic["required"] and semantic["status"] != "passed":
        findings = [
            *findings,
            Finding(
                code="content.review_pending",
                severity="warning",
                message="Завершите content-review для текущей версии: plan, requirements, final.",
            ),
        ]
    elif not semantic["required"]:
        findings = [
            *findings,
            Finding(
                code="content.review_disabled",
                severity="warning",
                message="В старой папке содержательная проверка не обязательна. Включение: content-review init.",
            ),
        ]
    status = (
        "preview"
        if preview
        else "blocked"
        if any(f.severity == "error" for f in findings)
        else "review"
        if set(candidates) - set(reviewed) or (semantic["required"] and semantic["status"] != "passed")
        else "ready"
    )
    report = CheckReport(
        status=status,
        fingerprint=key,
        artifact_hashes=hashes,
        findings=findings,
        page_count=pdf_state["page_count"],
        review_pages=candidates,
        reviewed_pages=reviewed,
        automatic_checks=[
            "схема и структура",
            "политики ссылок",
            "целостность исходников",
            "стили и поля DOCX",
            "номера страниц и оглавление",
            "геометрия и текст PDF",
            "шрифт PDF",
        ],
        checked_at=now(),
        technical_status="blocked" if any(f.severity == "error" for f in findings) else "passed",
        content_review=semantic,
    )
    save_report(workspace, report)
    return report


def check(workspace: Path) -> CheckReport:
    with workspace_lock(workspace):
        return _check(workspace)


def _check(workspace: Path) -> CheckReport:
    manifest, template, profile, content = load_workspace(workspace)
    findings = validate_inputs(workspace, manifest, template, content)
    state_path = workspace / ".cache" / "build.json"
    if not state_path.exists():
        return _blocked(
            workspace,
            [*findings, Finding(code="build.missing", message="Документ ещё не собран. Выполните build.")],
        )
    state = read_json(state_path)
    key, _ = fingerprint(workspace, template)
    if key != state["fingerprint"]:
        return _blocked(
            workspace,
            [
                *findings,
                Finding(
                    code="build.stale",
                    message="Содержание, настройки или движок изменились после сборки. Выполните build.",
                ),
            ],
            key,
        )
    if not _artifacts_match(workspace / "output", state["artifact_hashes"]):
        return _blocked(
            workspace,
            [
                *findings,
                Finding(
                    code="build.modified",
                    message="Итоговый DOCX/PDF изменён или отсутствует. Выполните build для восстановления.",
                ),
            ],
            key,
        )
    findings.extend(check_docx(workspace / "output" / "document.docx", template, content))
    pdf_state = inspect_pdf(
        workspace / "output" / "document.pdf", content, template, workspace / "output" / "pages"
    )
    findings.extend(pdf_state.pop("findings"))
    page_overview(workspace)
    preview = bool(state.get("preview", False))
    if preview:
        findings.append(
            Finding(
                code="build.preview",
                message="Текущий результат является предпросмотром черновика и не готов к публикации.",
                severity="info",
            )
        )
    return _report(
        workspace, key, state["artifact_hashes"], findings, pdf_state, profile.verification, preview=preview
    )


def review(workspace: Path, page: int, note: str) -> CheckReport:
    if not note.strip():
        raise ValueError("Укажите, что фактически проверено на странице.")
    with workspace_lock(workspace):
        report = _check(workspace)
        if report.status == "preview":
            raise ValueError("Страницы предпросмотра черновика нельзя отмечать как окончательно проверенные.")
        if report.status == "blocked":
            raise ValueError("Сначала устраните ошибки и соберите текущую версию документа.")
        if page < 1 or page > report.page_count:
            raise ValueError("Такой страницы нет.")
        path = workspace / ".cache" / "reviews.json"
        reviews = read_json(path) if path.exists() else {}
        reviews[str(page)] = {
            "page_hash": digest(workspace / "output" / "pages" / f"page-{page}.png"),
            "note": note,
            "reviewed_at": now(),
        }
        write_json(path, reviews)
        return _check(workspace)


def patch_block(workspace: Path, block_id: str, text: str):
    with workspace_lock(workspace):
        data = read_json(workspace / "document.json")
        found = False
        for block in data["blocks"]:
            if block["id"] == block_id:
                if block["type"] not in {"paragraph", "heading", "equation", "appendix"}:
                    raise ValueError(
                        "Команда patch предназначена для текстовых блоков. Таблицу/изображение измените в document.json."
                    )
                block["text"] = text
                found = True
        if not found:
            raise ValueError(f"Блок не найден: {block_id}")
        content = DocumentContent.model_validate(data)
        write_json(workspace / ".cache" / "document.previous.json", read_json(workspace / "document.json"))
        write_json(workspace / "document.json", content)
        # Immediately invalidate the visible report, even before the next check.
        _blocked(workspace, [Finding(code="build.stale", message="Текст изменён. Выполните build.")])


def publish_template(workspace: Path, library: Library | None = None) -> Path:
    library = library or Library()
    report = check(workspace)
    manifest, template, _, _ = load_workspace(workspace)
    if manifest.mode != "template":
        raise ValueError("Для публикации используйте рабочую папку анализа шаблона.")
    if (
        report.status != "ready"
        or template.status != "ready"
        or template.unresolved
        or not template.review_note.strip()
    ):
        raise ValueError(
            "Публикация требует разрешённых вопросов, review_note и полностью проверенного образца."
        )
    for i, source in enumerate(template.provenance):
        if source.location and source.sha256:
            original = contained(workspace, source.location)
            target = library.directory / "template-assets" / template.id / f"{i}-{original.name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
            source.location = str(target.resolve())
    return library.save_template(template)

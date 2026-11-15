"""Prepare an atomic, self-describing workspace; never invoke an AI agent."""

import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from codexdoc import __version__
from codexdoc.analysis import analyze
from codexdoc.instructions import generate_agents
from codexdoc.library import Library
from codexdoc.models import (
    AgentProfile,
    ContentAssessment,
    ContentBaseline,
    DocumentContent,
    ExcludedSource,
    Metadata,
    Provenance,
    Source,
    Template,
    Workspace,
)
from codexdoc.storage import (
    ROOT,
    atomic_text,
    contained,
    digest,
    now,
    read_json,
    safe_name,
    workspace_lock,
    write_json,
)

SOURCE_ROLE_DIRECTORY = {
    "citable": "sources",
    "context_only": "context",
    "requirements_style": "template",
}


def load_workspace(path: Path) -> tuple[Workspace, Template, AgentProfile, DocumentContent]:
    return (
        Workspace.model_validate(read_json(path / "workspace.json")),
        Template.model_validate(read_json(path / "template.json")),
        AgentProfile.model_validate(read_json(path / "profile.json")),
        DocumentContent.model_validate(read_json(path / "document.json")),
    )


def task_text(title: str, requirements: str) -> str:
    return f"# {title}\n\n{requirements}\n"


def refresh_instructions(path: Path) -> Path:
    """Write current engine guidance beside an older AGENTS.md without replacing it."""
    path = path.resolve()
    manifest, template, profile, _ = load_workspace(path)
    destination = path / f"AGENTS.codexdoc-{__version__}.md"
    content = generate_agents(manifest, template, profile, path)
    if destination.exists():
        if destination.read_text(encoding="utf-8") != content:
            raise FileExistsError(
                f"{destination.name} уже существует с другим содержимым; сохраните его перед обновлением."
            )
    else:
        atomic_text(destination, content)
    write_json(path / "schemas" / "template.schema.json", Template.model_json_schema())
    write_json(path / "schemas" / "document.schema.json", DocumentContent.model_json_schema())
    return destination


def workspace_inventory(path: Path) -> dict:
    """List source, norm and asset files with their declared role and integrity state."""
    path = path.resolve()
    manifest, template, _, content = load_workspace(path)
    tracked: dict[str, list[dict]] = {}

    def add(relative: str, entry: dict):
        if relative:
            key = Path(relative).as_posix()
            tracked.setdefault(key, []).append(entry)

    for source in manifest.sources:
        if source.path:
            add(
                source.path,
                {
                    "kind": "source",
                    "id": source.id,
                    "name": source.name,
                    "role": source.role,
                    "sha256": source.sha256,
                },
            )
    invalid_provenance = []
    for provenance in template.provenance:
        if not provenance.location:
            continue
        try:
            source_path = contained(path, provenance.location)
        except ValueError:
            invalid_provenance.append(
                {"title": provenance.title, "location": provenance.location, "status": "outside_workspace"}
            )
            continue
        add(
            source_path.relative_to(path).as_posix(),
            {
                "kind": "provenance",
                "title": provenance.title,
                "scope": provenance.scope,
                "sha256": provenance.sha256,
            },
        )
    for block in content.blocks:
        if block.type == "image":
            add(block.path, {"kind": "document_asset", "block_id": block.id})

    files = []
    for root_name in ("sources", "context", "template", "norms", "assets"):
        root = path / root_name
        if not root.is_dir():
            continue
        for file in sorted(p for p in root.rglob("*") if p.is_file()):
            relative = file.relative_to(path).as_posix()
            declarations = tracked.get(relative, [])
            actual_hash = digest(file)
            expected_hashes = sorted({item["sha256"] for item in declarations if item.get("sha256")})
            mismatch = len(expected_hashes) > 1 or bool(
                expected_hashes and actual_hash not in expected_hashes
            )
            files.append(
                {
                    "path": relative,
                    "size_bytes": file.stat().st_size,
                    "sha256": actual_hash,
                    "declared_sha256": expected_hashes,
                    "status": "hash_mismatch" if mismatch else "tracked" if declarations else "untracked",
                    "declared_as": declarations,
                }
            )

    present = {item["path"] for item in files}
    missing = sorted(relative for relative in tracked if relative not in present)
    return {
        "files": files,
        "missing": missing,
        "invalid_provenance": invalid_provenance,
        "untracked": [item["path"] for item in files if item["status"] == "untracked"],
        "hash_mismatches": [item["path"] for item in files if item["status"] == "hash_mismatch"],
        "counts": {
            root: sum(item["path"].startswith(root + "/") for item in files)
            for root in ("sources", "context", "template", "norms", "assets")
        },
    }


def create_workspace(
    parent: Path,
    metadata: Metadata,
    template: Template,
    profile: AgentProfile,
    materials: list[tuple[Path, Source]],
    requirements: str = "",
    mode: Literal["document", "template"] = "document",
    library: Library | None = None,
    excluded_sources: list[ExcludedSource] | None = None,
) -> Path:
    library = library or Library()
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / safe_name(metadata.title)
    if destination.exists():
        destination = parent / f"{safe_name(metadata.title)}-{uuid.uuid4().hex[:6]}"
    staging = Path(tempfile.mkdtemp(prefix=".preparing-", dir=parent))
    try:
        sources = []
        excluded = list(excluded_sources or [])
        used_ids = {item.id for item in excluded}
        next_source_index = 1
        analyses = []

        def new_source_id(preferred: str = "") -> str:
            nonlocal next_source_index
            if preferred and preferred != "pending" and preferred not in used_ids:
                used_ids.add(preferred)
                return preferred
            while f"s{next_source_index:03}" in used_ids:
                next_source_index += 1
            value = f"s{next_source_index:03}"
            used_ids.add(value)
            next_source_index += 1
            return value

        for original, settings in materials:
            source = settings.model_copy(deep=True)
            if source._legacy_excluded:
                excluded.append(
                    ExcludedSource(
                        id=new_source_id(source.id),
                        name=original.name,
                        kind="material",
                        confidential=source.confidential,
                    )
                )
                continue
            source.id = new_source_id(source.id)
            source.name = original.name
            folder = SOURCE_ROLE_DIRECTORY[source.role]
            source.path = f"{folder}/{source.id}-{safe_name(original.name)}"
            target = staging / source.path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
            source.sha256 = digest(target)
            result = analyze(target, library.directory / "cache")
            source.analysis = f"analysis/{source.id}.json"
            write_json(staging / source.analysis, result)
            analyses.append(
                {
                    "source_id": source.id,
                    "name": source.name,
                    "role": source.role,
                    "title_metadata": source.title_metadata,
                    **result,
                }
            )
            sources.append(source)

        template = template.model_copy(deep=True)
        for evidence in template.provenance:
            original = Path(evidence.location)
            if not original.is_absolute():
                original = ROOT / evidence.location
            if original.is_file():
                norm_target = staging / "norms" / safe_name(original.name)
                norm_target.parent.mkdir(parents=True, exist_ok=True)
                if norm_target.exists() and digest(norm_target) != digest(original):
                    norm_target = norm_target.with_name(f"{digest(original)[:10]}-{norm_target.name}")
                shutil.copy2(original, norm_target)
                evidence.location = norm_target.relative_to(staging).as_posix()
                evidence.sha256 = digest(norm_target)

        if mode == "template":
            formatting_analyses = [a for a in analyses if not a["title_metadata"]]
            template = derive_draft(template, formatting_analyses)
            write_json(
                staging / "analysis" / "comparison.json",
                {
                    "status": "needs_agent_analysis",
                    "questions": template.unresolved,
                    "samples": [
                        {k: a[k] for k in ["source_id", "name", "role", "sha256", "measured", "warnings"]}
                        for a in formatting_analyses
                    ],
                },
            )
            template.provenance.extend(
                Provenance(
                    title=s.name,
                    location=s.path,
                    sha256=s.sha256,
                    scope=(
                        "Разрешённый источник для изучения и цитирования."
                        if s.role == "citable"
                        else "Только анализ оформления."
                    ),
                )
                for s in sources
                if s.path and s.role in {"citable", "requirements_style"} and not s.title_metadata
            )
        manifest = Workspace(
            engine_version=__version__,
            name=metadata.title,
            mode=mode,
            sources=sources,
            excluded_sources=excluded,
            requirements=requirements,
            content_review_required=True,
            created_at=now(),
        )
        write_json(staging / "workspace.json", manifest)
        write_json(
            staging / "analysis" / "index.json",
            [
                {
                    "source_id": s.id,
                    "name": s.name,
                    "role": s.role,
                    "analysis": s.analysis,
                    "sha256": s.sha256,
                }
                for s in sources
            ],
        )
        write_json(staging / "template.json", template)
        write_json(staging / "profile.json", profile)
        write_json(staging / "document.json", DocumentContent(metadata=metadata))
        write_json(staging / "schemas" / "document.schema.json", DocumentContent.model_json_schema())
        write_json(staging / "schemas" / "template.schema.json", Template.model_json_schema())
        write_json(staging / "schemas" / "workspace.schema.json", Workspace.model_json_schema())
        write_json(staging / "schemas" / "baseline.schema.json", ContentBaseline.model_json_schema())
        write_json(staging / "schemas" / "content-review.schema.json", ContentAssessment.model_json_schema())
        write_json(staging / "analysis" / "baseline.json", {"goal": "", "scope": "", "items": []})
        write_json(staging / "schemas" / "blocks.example.json", example_content().model_dump())
        atomic_text(staging / "AGENTS.md", generate_agents(manifest, template, profile, destination))
        atomic_text(staging / "INSTRUCTIONS.md", generate_agents(manifest, template, profile, destination))
        atomic_text(staging / "task.md", task_text(manifest.name, requirements))
        # No source contents or user text are interpolated into executable commands.
        write_json(
            staging / "engine.json", {"root": str(ROOT), "python": sys.executable, "version": __version__}
        )
        launcher = (
            '@echo off\r\nchcp 65001 >nul\r\nset PYTHONUTF8=1\r\n"'
            + sys.executable.replace("%", "%%")
            + '" "'
            + str(ROOT / "scripts" / "workspace_cli.py").replace("%", "%%")
            + '" "%~dp0." %*\r\n'
        )
        (staging / "doc.cmd").write_bytes(launcher.encode("utf-8"))
        for folder in ("sources", "context", "template", "assets", "output"):
            (staging / folder).mkdir(exist_ok=True)
        os.replace(staging, destination)
        return destination
    except BaseException:
        shutil.rmtree(staging)
        raise


def update_workspace(
    path: Path,
    metadata: Metadata,
    template: Template,
    profile: AgentProfile,
    materials: list[tuple[Path, Source]],
    requirements: str = "",
    mode: Literal["document", "template"] = "document",
    library: Library | None = None,
    excluded_sources: list[ExcludedSource] | None = None,
) -> Path:
    """Update app-managed workspace inputs while preserving agent-authored blocks."""
    library = library or Library()
    path = path.resolve()
    if not (path / "workspace.json").is_file():
        raise ValueError("В выбранной папке нет workspace.json.")

    with workspace_lock(path):
        old_manifest, old_template, old_profile, old_content = load_workspace(path)
        task_path = path / "task.md"
        if task_path.is_file() and task_path.read_text(encoding="utf-8") != task_text(
            old_manifest.name, old_manifest.requirements
        ):
            raise ValueError(
                "task.md изменён вне приложения. Перенесите его текст в задание проекта и сохраните внешнюю копию перед обновлением папки."
            )
        old_sources = {source.id: source for source in old_manifest.sources}
        old_generated = generate_agents(old_manifest, old_template, old_profile, path)
        existing_agents_path = path / "AGENTS.md"
        existing_agents = (
            existing_agents_path.read_text(encoding="utf-8") if existing_agents_path.is_file() else ""
        )

        excluded = list(excluded_sources or [])
        active_sources: list[Source] = []
        source_ids: set[str] = {item.id for item in excluded}
        next_index = 1

        def allocate_id(preferred: str = "") -> str:
            nonlocal next_index
            if preferred and preferred not in source_ids:
                source_ids.add(preferred)
                return preferred
            while f"s{next_index:03}" in source_ids:
                next_index += 1
            value = f"s{next_index:03}"
            source_ids.add(value)
            next_index += 1
            return value

        old_paths = {source.path for source in old_manifest.sources if source.path}
        new_paths: set[str] = set()
        new_source_paths: dict[str, tuple[str, str]] = {}
        analysis_index = []
        removed_paths: list[Path] = []

        for original, settings in materials:
            if not original.is_file():
                raise ValueError(f"Файл материала не найден: {original}")
            source = settings.model_copy(deep=True)
            if source._legacy_excluded:
                excluded.append(
                    ExcludedSource(
                        id=allocate_id(source.id),
                        name=original.name,
                        kind="material",
                        confidential=source.confidential,
                    )
                )
                continue

            source_id = allocate_id(source.id if source.id != "pending" else "")
            old_source = old_sources.get(source_id)
            source.id = source_id
            folder = SOURCE_ROLE_DIRECTORY[source.role]
            source.name = old_source.name if old_source else settings.name or original.name
            relative = f"{folder}/{source.id}-{safe_name(source.name)}"
            target = contained(path, relative)
            if target.exists() and relative not in old_paths and target.resolve() != original.resolve():
                relative = f"{folder}/{source.id}-{digest(original)[:8]}-{safe_name(source.name)}"
                target = contained(path, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            if original.resolve() != target.resolve():
                temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                try:
                    shutil.copy2(original, temporary)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)

            current_hash = digest(target)
            source.path = relative
            source.sha256 = current_hash
            source.analysis = f"analysis/{source.id}.json"
            analysis_path = contained(path, source.analysis)
            if old_source and old_source.sha256 == current_hash and analysis_path.is_file():
                result = read_json(analysis_path)
            else:
                result = analyze(target, library.directory / "cache")
                write_json(analysis_path, result)
            active_sources.append(source)
            new_paths.add(relative)
            new_source_paths[source.id] = (relative, current_hash)
            analysis_index.append(
                {
                    "source_id": source.id,
                    "name": source.name,
                    "role": source.role,
                    "analysis": source.analysis,
                    "sha256": source.sha256,
                }
            )

        excluded_ids = {item.id for item in excluded}
        active_sources = [source for source in active_sources if source.id not in excluded_ids]
        active_ids = {source.id for source in active_sources}
        analysis_index = [item for item in analysis_index if item["source_id"] in active_ids]
        for old_source in old_manifest.sources:
            if old_source.path and old_source.path not in new_paths:
                old_path = contained(path, old_source.path)
                if old_source.path.split("/", 1)[0] in SOURCE_ROLE_DIRECTORY.values():
                    removed_paths.append(old_path)
            if old_source.id not in active_ids and old_source.analysis:
                old_analysis = contained(path, old_source.analysis)
                if old_source.analysis.startswith("analysis/"):
                    removed_paths.append(old_analysis)

        template = template.model_copy(deep=True)
        remapped_provenance = []
        old_path_to_id = {source.path: source.id for source in old_manifest.sources if source.path}
        for evidence in template.provenance:
            source_id = old_path_to_id.get(evidence.location)
            if source_id is not None:
                if source_id not in new_source_paths:
                    continue
                evidence.location, evidence.sha256 = new_source_paths[source_id]
            remapped_provenance.append(evidence)
        template.provenance = remapped_provenance

        for evidence in template.provenance:
            if not evidence.location:
                continue
            original = Path(evidence.location)
            if not original.is_absolute():
                local = path / original
                original = local if local.is_file() else ROOT / original
            if original.is_file() and not original.resolve().is_relative_to(path):
                norm_target = path / "norms" / safe_name(original.name)
                if norm_target.exists() and digest(norm_target) != digest(original):
                    norm_target = norm_target.with_name(f"{digest(original)[:10]}-{norm_target.name}")
                if original.resolve() != norm_target.resolve():
                    norm_target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(original, norm_target)
                evidence.location = norm_target.relative_to(path).as_posix()
                evidence.sha256 = digest(norm_target)

        manifest = old_manifest.model_copy(
            update={
                "engine_version": __version__,
                "name": metadata.title,
                "mode": mode,
                "sources": active_sources,
                "excluded_sources": excluded,
                "requirements": requirements,
            },
            deep=True,
        )
        old_content.metadata = metadata
        write_json(path / "workspace.json", manifest)
        write_json(path / "template.json", template)
        write_json(path / "profile.json", profile)
        write_json(path / "document.json", old_content)
        write_json(path / "analysis" / "index.json", analysis_index)
        write_json(path / "schemas" / "workspace.schema.json", Workspace.model_json_schema())
        write_json(path / "schemas" / "document.schema.json", DocumentContent.model_json_schema())
        write_json(path / "schemas" / "template.schema.json", Template.model_json_schema())

        generated = generate_agents(manifest, template, profile, path)
        instructions_path = path / "INSTRUCTIONS.md"
        if not instructions_path.exists() or instructions_path.read_text(encoding="utf-8") == old_generated:
            atomic_text(instructions_path, generated)
        atomic_text(task_path, task_text(manifest.name, requirements))
        if not existing_agents or existing_agents == old_generated:
            atomic_text(existing_agents_path, generated)
        elif existing_agents != generated:
            versioned = path / f"AGENTS.codexdoc-{__version__}.md"
            previous_versioned = versioned.read_text(encoding="utf-8") if versioned.is_file() else ""
            if not previous_versioned or previous_versioned == old_generated:
                atomic_text(versioned, generated)
            elif previous_versioned != generated:
                versioned = path / f"AGENTS.codexdoc-{__version__}-{now()[:10]}.md"
                if versioned.exists() and versioned.read_text(encoding="utf-8") != generated:
                    versioned = path / f"AGENTS.codexdoc-{__version__}-{uuid.uuid4().hex[:10]}.md"
                atomic_text(versioned, generated)

        for removed in removed_paths:
            removed.unlink(missing_ok=True)

    return path


def derive_draft(base: Template, analyses: list[dict]) -> Template:
    draft = base.model_copy(deep=True)
    draft.id = "custom-" + uuid.uuid4().hex[:10]
    draft.name = "Шаблон по образцам"
    draft.status = "draft"
    draft.unresolved.append(
        "Агент должен проверить структуру, титул, колонтитулы, подписи и исключения; затем подтвердить представимость оформления схемой."
    )
    measured = []
    for result in analyses:
        for section in result["measured"].get("sections", []):
            measured.append(section)
        if result["format"] == ".pdf":
            draft.unresolved.append(
                f"{result['name']}: поля, стили и структуру нельзя однозначно восстановить из PDF."
            )
    if measured:
        first = measured[0]
        valid = {key: value for key, value in first.items() if value is not None}
        draft.page = draft.page.model_validate({**draft.page.model_dump(), **valid})
        for key in first:
            values = sorted({section[key] for section in measured if section.get(key) is not None})
            if len(values) > 1:
                draft.unresolved.append(
                    f"Различаются {key}: {values}. Это допустимые разные секции или конфликт образцов? Уточнить."
                )
    for result in analyses:
        style = result["measured"].get("normal_style")
        if style:
            candidate = {key: value for key, value in style.items() if value is not None}
            draft.text = draft.text.model_validate({**draft.text.model_dump(), **candidate})
            break
    styles = [a["measured"].get("normal_style", {}) for a in analyses]
    for key in ("font", "size_pt", "line_spacing", "indent_mm"):
        values = {str(s[key]) for s in styles if s.get(key) is not None}
        if len(values) > 1:
            draft.unresolved.append(f"Различаются параметры текста {key}: {', '.join(sorted(values))}.")
    draft.agent_notes = "Измерения являются исходными данными, а не завершённым анализом. После проверки добавь выявленные правила и границы применимости."
    return draft


def source_settings(path: Path, role: str = "context_only") -> Source:
    return Source(id="pending", name=path.name, role=role)


def example_content() -> DocumentContent:
    return DocumentContent.model_validate(
        {
            "metadata": {
                "title": "Проверка документного конвейера",
                "kind": "Технический отчёт",
                "organization": "Локальная испытательная среда",
                "author": "Автор документа",
                "city": "Москва",
                "year": "2026",
            },
            "blocks": [
                {"id": "intro", "type": "heading", "text": "Введение", "numbered": False},
                {
                    "id": "intro-text",
                    "type": "paragraph",
                    "text": "Цель проверки состоит в подтверждении согласованности редактируемого документа и его печатного представления. В документ включены заголовки, таблица, формула и приложение, чтобы проверить основные элементы оформления.",
                },
                {"id": "method", "type": "heading", "text": "Методика проверки"},
                {
                    "id": "method-text",
                    "type": "paragraph",
                    "text": "Проверка включает построение документа, обновление оглавления, экспорт в PDF и анализ границ текстовых блоков. Результаты сохраняются вместе с изображениями страниц. При изменении исходного содержания прежняя отметка о просмотре теряет силу.",
                },
                {
                    "id": "steps",
                    "type": "list",
                    "ordered": True,
                    "items": [
                        "Подготовить исходное содержание.",
                        "Собрать документ и проверить оформление.",
                        "Просмотреть страницы с потенциальными проблемами.",
                    ],
                },
                {
                    "id": "results",
                    "type": "table",
                    "caption": "Контрольные элементы",
                    "rows": [
                        ["Элемент", "Назначение"],
                        ["Заголовок", "Структура документа и оглавление"],
                        ["Таблица", "Сопоставление однородных данных"],
                        ["Формула", "Редактируемая математическая запись"],
                    ],
                },
                {
                    "id": "formula-intro",
                    "type": "paragraph",
                    "text": "Для проверки математической записи используется простое соотношение пути, скорости и времени. Значения переменных в данном примере не задаются.",
                },
                {"id": "formula", "type": "equation", "text": "s = v · t"},
                {"id": "conclusion", "type": "heading", "text": "Заключение", "numbered": False},
                {
                    "id": "conclusion-text",
                    "type": "paragraph",
                    "text": "Подготовленный набор элементов позволяет проверить базовое оформление страниц и работу оглавления. Окончательная оценка результата требует просмотра печатного представления и проверки соответствия требованиям конкретного задания.",
                },
                {"id": "appendix", "type": "appendix", "text": "Дополнительные сведения"},
                {
                    "id": "appendix-text",
                    "type": "paragraph",
                    "text": "Приложение используется для размещения вспомогательных материалов, которые дополняют основную часть документа. Этот текст предназначен для проверки отдельного заголовка и перехода на новую страницу.",
                },
            ],
        }
    )

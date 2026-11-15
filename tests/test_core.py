import json
import sys
from pathlib import Path

import pymupdf
import pytest
from docx import Document
from docx.shared import Mm
from pydantic import ValidationError

from codexdoc import __version__
from codexdoc.analysis import analyze
from codexdoc.checks import inspect_pdf, validate_inputs
from codexdoc.cli import main, parser
from codexdoc.docx_engine import compose
from codexdoc.extensions import run_extension
from codexdoc.instructions import generate_agents
from codexdoc.models import AgentProfile, DocumentContent, Metadata, Source, Template, Workspace
from codexdoc.pipeline import patch_block
from codexdoc.storage import contained, digest, workspace_lock, write_json
from codexdoc.workspaces import (
    create_workspace,
    derive_draft,
    load_workspace,
    refresh_instructions,
    source_settings,
    update_workspace,
    workspace_inventory,
)


def test_title_sample_does_not_change_template_formatting(tmp_path, library):
    sample = tmp_path / "title.docx"
    document = Document()
    document.sections[0].left_margin = Mm(60)
    document.add_paragraph("Автор: Иванов И.И.")
    document.save(str(sample))
    original = library.get_template("report")
    workspace = create_workspace(
        tmp_path / "tasks",
        Metadata(title="Текущая работа"),
        original,
        AgentProfile(),
        [(sample, Source(id="pending", name=sample.name, policy="context", title_metadata=True))],
        mode="template",
        library=library,
    )
    manifest, template, profile, _ = load_workspace(workspace)
    assert template.page.left_mm == original.page.left_mm
    comparison = json.loads((workspace / "analysis" / "comparison.json").read_text(encoding="utf-8"))
    assert comparison["samples"] == []
    assert "## Титульные данные из образца" in generate_agents(manifest, template, profile)
    refreshed = refresh_instructions(workspace)
    assert "## Титульные данные из образца" in refreshed.read_text(encoding="utf-8")


def test_source_policies_and_atomic_workspace(tmp_path, library):
    files = []
    for index, policy in enumerate(("cite", "context", "format", "exclude")):
        file = tmp_path / f"Материал {index}.txt"
        file.write_text("Исходный текст", encoding="utf-8")
        files.append((file, Source(id="pending", name=file.name, policy=policy)))
    workspace = create_workspace(
        tmp_path / "tasks",
        Metadata(title="Отчёт"),
        library.get_template("report"),
        AgentProfile(),
        files,
        library=library,
    )
    manifest, template, profile, _ = load_workspace(workspace)
    assert len(list((workspace / "sources").iterdir())) == 1
    assert len(list((workspace / "context").iterdir())) == 1
    assert len(list((workspace / "template").iterdir())) == 1
    assert [source.role for source in manifest.sources] == ["citable", "context_only", "requirements_style"]
    assert len(manifest.excluded_sources) == 1
    assert manifest.excluded_sources[0].name == "Материал 3.txt"
    assert manifest.sources[1].analysis
    instructions = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert len(instructions) < 10000
    assert "`context_only` можно изучать без ссылок и любых упоминаний файла" in instructions
    assert "workspace.mode задаёт технический процесс" in instructions
    assert "не подменяй заказанный отчёт каркасом" in instructions
    assert "сначала запроси разрешение" in instructions
    assert "Не используй интернет" not in instructions
    assert instructions == generate_agents(manifest, template, profile, workspace)
    assert str(workspace.resolve()) in instructions
    assert ".preparing-" not in instructions
    assert "title_layout" in instructions and "contents_format" in instructions
    assert "metadata.authors" in instructions and "обычный build" in instructions
    assert not list((tmp_path / "tasks").glob(".preparing-*"))


def test_legacy_workspace_source_policies_migrate_to_single_roles():
    legacy = Workspace.model_validate(
        {
            "engine_version": "0.3.0",
            "name": "Старое задание",
            "created_at": "2026-01-01T00:00:00+00:00",
            "sources": [
                {
                    "id": "s001",
                    "name": "article.pdf",
                    "path": "sources/s001-article.pdf",
                    "policy": "cite",
                    "role": "material",
                },
                {
                    "id": "s002",
                    "name": "context.txt",
                    "path": "sources/s002-context.txt",
                    "policy": "context",
                    "role": "code",
                },
                {
                    "id": "s003",
                    "name": "style.docx",
                    "path": "sources/s003-style.docx",
                    "policy": "format",
                    "role": "sample",
                },
                {
                    "id": "s004",
                    "name": "excluded.pdf",
                    "path": "",
                    "policy": "exclude",
                    "role": "sample",
                    "confidential": True,
                },
            ],
        }
    )

    assert [source.role for source in legacy.sources] == ["citable", "context_only", "requirements_style"]
    assert len(legacy.excluded_sources) == 1
    assert legacy.excluded_sources[0].name == "excluded.pdf"
    assert legacy.excluded_sources[0].confidential is True
    dumped = legacy.model_dump(mode="json")
    assert all("policy" not in source for source in dumped["sources"])


def test_update_workspace_preserves_agent_blocks_and_moves_source_bucket(tmp_path, library):
    original = tmp_path / "source.txt"
    original.write_text("Исходный материал", encoding="utf-8")
    workspace = create_workspace(
        tmp_path / "tasks",
        Metadata(title="Исходное задание"),
        library.get_template("lab"),
        AgentProfile(),
        [(original, Source(id="pending", name=original.name, role="citable"))],
        library=library,
    )
    manifest, template, profile, document = load_workspace(workspace)
    source_path = workspace / manifest.sources[0].path
    document.blocks = [
        {"id": "intro", "type": "heading", "text": "Введение", "level": 1, "numbered": False},
        {"id": "body", "type": "paragraph", "text": "Текст, подготовленный агентом."},
    ]
    write_json(workspace / "document.json", document)

    update_workspace(
        workspace,
        Metadata(title="Обновлённое задание", author="Иванов И.И."),
        template,
        profile,
        [(source_path, Source(id=manifest.sources[0].id, name=original.name, role="context_only"))],
        requirements="Не упоминать внутренний контекст.",
        library=library,
    )

    updated_manifest, _, _, updated_document = load_workspace(workspace)
    assert updated_manifest.name == "Обновлённое задание"
    assert updated_manifest.requirements == "Не упоминать внутренний контекст."
    assert updated_manifest.sources[0].role == "context_only"
    assert updated_manifest.sources[0].path.startswith("context/")
    assert not source_path.exists()
    assert updated_document.blocks[1].text == "Текст, подготовленный агентом."
    assert updated_document.metadata.author == "Иванов И.И."


def test_update_does_not_overwrite_edited_task(workspace, library):
    manifest, template, profile, content = load_workspace(workspace)
    task = workspace / "task.md"
    task.write_text("Пользовательское изменение", encoding="utf-8")
    before = (workspace / "workspace.json").read_bytes()
    with pytest.raises(ValueError, match="task.md изменён"):
        from codexdoc.workspaces import update_workspace

        update_workspace(
            workspace, content.metadata, template, profile, [], "Новые требования", library=library
        )
    assert task.read_text(encoding="utf-8") == "Пользовательское изменение"
    assert (workspace / "workspace.json").read_bytes() == before


def test_title_fields_does_not_guess_ambiguous_values():
    from codexdoc.studio_state import title_fields

    assert title_fields(
        "Группа: КТ-01\nАвтор: Первый\nАвтор: Второй\nГод: 2026\nНазвание: Старая работа"
    ) == {"group": "КТ-01", "year": "2026"}


def test_template_workspace_tracks_inherited_norms_and_omits_context_provenance(tmp_path, library):
    format_file = tmp_path / "format.txt"
    context_file = tmp_path / "private-context.txt"
    format_file.write_text("Оформление", encoding="utf-8")
    context_file.write_text("Контекст", encoding="utf-8")
    workspace = create_workspace(
        tmp_path / "tasks",
        Metadata(title="Анализ шаблона"),
        library.get_template("report"),
        AgentProfile(),
        [
            (format_file, Source(id="pending", name=format_file.name, policy="format")),
            (context_file, Source(id="pending", name=context_file.name, policy="context")),
        ],
        mode="template",
        library=library,
    )
    _, template, _, _ = load_workspace(workspace)
    provenance_names = {item.title for item in template.provenance}
    assert "private-context.txt" not in provenance_names
    assert any(item.location.startswith("norms/") for item in template.provenance)
    inventory = workspace_inventory(workspace)
    assert inventory["untracked"] == []
    assert inventory["hash_mismatches"] == []
    comparison = json.loads((workspace / "analysis/comparison.json").read_text(encoding="utf-8"))
    assert [sample["role"] for sample in comparison["samples"]] == ["requirements_style", "context_only"]


def test_workspace_inventory_and_validation_detect_orphaned_norms(workspace):
    orphan = workspace / "norms" / "unlisted.pdf"
    orphan.parent.mkdir(exist_ok=True)
    orphan.write_bytes(b"untracked normative file")
    inventory = workspace_inventory(workspace)
    assert inventory["untracked"] == ["norms/unlisted.pdf"]
    manifest, template, _, content = load_workspace(workspace)
    assert "norm.untracked" in {
        finding.code for finding in validate_inputs(workspace, manifest, template, content)
    }


def test_cli_accepts_draft_preview_and_local_source_page_selection(tmp_path):
    args = parser().parse_args(["build", str(tmp_path), "--preview"])
    assert args.preview
    args = parser().parse_args(
        ["source-preview", str(tmp_path), "--id", "s001", "--page", "1", "--page", "3"]
    )
    assert args.pages == [1, 3]


def test_inspect_warns_when_workspace_instructions_may_be_stale(workspace, capsys):
    manifest, template, _, _ = load_workspace(workspace)
    manifest.engine_version = "0.1.0"
    template.engine_version = "0.1.0"
    write_json(workspace / "workspace.json", manifest)
    write_json(workspace / "template.json", template)

    assert main(["inspect", str(workspace)]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["engine"]["prepared_with"] == "0.1.0"
    assert report["engine"]["instructions_may_be_outdated"] is True
    assert report["engine"]["current"] == __version__
    assert report["template"]["needs_revalidation"] is True
    assert report["checks_current"] is False


def test_refresh_instructions_preserves_existing_agent_file(workspace, capsys):
    manifest, _, _, _ = load_workspace(workspace)
    manifest.engine_version = "0.1.0"
    write_json(workspace / "workspace.json", manifest)
    original = workspace / "AGENTS.md"
    original.write_text("Пользовательские старые инструкции", encoding="utf-8")

    updated = refresh_instructions(workspace)

    assert updated.name == f"AGENTS.codexdoc-{__version__}.md"
    assert "workspace.mode задаёт технический процесс" in updated.read_text(encoding="utf-8")
    assert str(workspace.resolve()) in updated.read_text(encoding="utf-8")
    assert "title_layout" in updated.read_text(encoding="utf-8")
    schema = json.loads((workspace / "schemas/template.schema.json").read_text(encoding="utf-8"))
    assert "title_layout" in schema["properties"]
    assert original.read_text(encoding="utf-8") == "Пользовательские старые инструкции"
    assert refresh_instructions(workspace) == updated
    assert main(["inspect", str(workspace)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["engine"]["current_instructions"] == f"AGENTS.codexdoc-{__version__}.md"


def test_failed_creation_preserves_original(tmp_path, library):
    missing = tmp_path / "missing.docx"
    with pytest.raises(FileNotFoundError):
        create_workspace(
            tmp_path / "tasks",
            Metadata(),
            library.get_template("lab"),
            AgentProfile(),
            [(missing, source_settings(missing))],
            library=library,
        )
    assert not list((tmp_path / "tasks").iterdir())


def test_duplicate_workspace_names_do_not_overwrite(tmp_path, library):
    args = (tmp_path, Metadata(title="Одинаковое имя"), library.get_template("lab"), AgentProfile(), [])
    one = create_workspace(*args, library=library)
    two = create_workspace(*args, library=library)
    assert one != two and one.is_dir() and two.is_dir()


def test_forbidden_unknown_and_unverified_citations(workspace):
    manifest, template, _, content = load_workspace(workspace)
    manifest.sources = [
        Source(id="s1", name="secret.docx", policy="context"),
        Source(id="s2", name="article", policy="cite"),
    ]
    paragraph = next(block for block in content.blocks if block.type == "paragraph")
    paragraph.citations = ["s1", "s2", "missing"]
    codes = {f.code for f in validate_inputs(workspace, manifest, template, content)}
    assert {"citation.forbidden", "citation.unverified", "citation.unknown"} <= codes


def test_source_integrity(workspace):
    manifest, template, _, content = load_workspace(workspace)
    path = workspace / "sources" / "test.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("original", encoding="utf-8")
    manifest.sources = [Source(id="s1", name=path.name, path="sources/test.txt", sha256=digest(path))]
    path.write_text("changed", encoding="utf-8")
    write_json(workspace / "workspace.json", manifest)
    assert "source.changed" in {f.code for f in validate_inputs(workspace, manifest, template, content)}
    assert "sources/test.txt" in workspace_inventory(workspace)["hash_mismatches"]


def test_schema_and_path_guards(workspace):
    with pytest.raises(ValueError):
        contained(workspace, "../outside.txt")
    with pytest.raises(ValidationError):
        DocumentContent.model_validate(
            {"blocks": [{"type": "table", "id": "bad", "caption": "x", "rows": [["one"], ["two", "three"]]}]}
        )
    with pytest.raises(ValidationError):
        DocumentContent.model_validate(
            {
                "blocks": [
                    {"type": "paragraph", "id": "same", "text": "a"},
                    {"type": "paragraph", "id": "same", "text": "b"},
                ]
            }
        )
    with pytest.raises(ValidationError):
        Template(id="../escape", name="Invalid")


def test_lock_refuses_concurrent_writers(workspace):
    with workspace_lock(workspace), pytest.raises(ValueError), workspace_lock(workspace):
        pass
    assert not (workspace / ".build.lock").exists()


def test_analysis_cache_and_conflicting_samples(tmp_path, library):
    results = []
    for i, left in enumerate([25, 30]):
        doc = Document()
        doc.sections[0].left_margin = Mm(left)
        doc.add_paragraph("Текст образца")
        path = tmp_path / f"sample{i}.docx"
        doc.save(path)
        result = analyze(path, tmp_path / "cache")
        assert analyze(path, tmp_path / "cache") == result
        results.append({**result, "name": path.name})
    draft = derive_draft(library.get_template("report"), results)
    assert draft.status == "draft"
    assert any("left_mm" in q for q in draft.unresolved)
    assert len(list((tmp_path / "cache").glob("*.json"))) == 2


def test_targeted_composition_preserves_unchanged_block_xml(workspace):
    manifest, template, _, content = load_workspace(workspace)
    first = compose(workspace, content, template, manifest.sources)
    doc = Document(str(workspace / ".cache" / "authored.docx"))
    start, end = first["spans"]["method-text"]
    unchanged = [element.xml for element in list(doc.element.body)[start:end]]
    patch_block(
        workspace, "intro-text", "Изменён только вводный абзац. Остальные элементы должны остаться прежними."
    )
    _, _, _, modified = load_workspace(workspace)
    second = compose(workspace, modified, template, manifest.sources)
    assert second["incremental"]
    assert second["changed_blocks"] == ["intro-text"]
    doc = Document(str(workspace / ".cache" / "authored.docx"))
    start, end = second["spans"]["method-text"]
    assert unchanged == [element.xml for element in list(doc.element.body)[start:end]]


def test_extensions_both_execution_modes(workspace, tmp_path):
    local = run_extension(workspace, "word-count")
    assert local.metrics["words"] > 0
    adapter = Path(__file__).resolve().parent.parent / "examples" / "word_count_adapter.py"
    config = tmp_path / "extensions.json"
    write_json(
        config, [{"name": "external-count", "version": "1", "command": [sys.executable, str(adapter)]}]
    )
    external = run_extension(workspace, "external-count", config=config)
    assert external.metrics["words"] == local.metrics["words"]
    assert run_extension(workspace, "external-count", config=config) == external


@pytest.mark.parametrize("internet", ["off", "ask", "allowed"])
def test_profiles_select_one_internet_policy(workspace, internet):
    manifest, template, _, _ = load_workspace(workspace)
    profile = AgentProfile(internet=internet)
    text = generate_agents(manifest, template, profile)
    phrases = [
        "Не используй интернет.",
        "Для поиска в интернете сначала запроси разрешение",
        "При необходимости ищи источники в интернете",
    ]
    assert sum(phrase in text for phrase in phrases) == 1


def test_template_library_revisions(library):
    template = library.get_template("lab").model_copy(update={"id": "custom-test"})
    library.save_template(template)
    template.name = "Другая редакция"
    library.save_template(template)
    assert library.get_template("custom-test").revision == 2
    assert (library.directory / "history/custom-test/1.json").exists()


def test_legacy_ready_template_requires_revalidation(library):
    legacy = Template(id="legacy-ready", name="Legacy", engine_version="0.1.0", status="ready")
    write_json(library.directory / "templates/legacy-ready.json", legacy)

    loaded = library.get_template("legacy-ready")

    assert loaded.engine_version == __version__
    assert loaded.status == "draft"
    assert any("Повторно проверьте унаследованный шаблон" in item for item in loaded.unresolved)
    derived = derive_draft(loaded, [])
    assert any("Повторно проверьте унаследованный шаблон" in item for item in derived.unresolved)
    assert library.save_template(loaded).is_file()


def test_generated_schema_is_valid_json(workspace):
    schema = json.loads((workspace / "schemas/document.schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["blocks"]["items"]["discriminator"]["propertyName"] == "type"


def test_invisible_pdf_text_is_a_blocking_error(tmp_path):
    pdf = pymupdf.open()
    page = pdf.new_page(width=595.28, height=841.89)
    page.insert_text((100, 100), "Invisible formula", fontsize=14, color=(1, 1, 1))
    path = tmp_path / "white.pdf"
    pdf.save(path)
    pdf.close()
    result = inspect_pdf(path, DocumentContent(), Template(id="test", name="Test"), tmp_path / "pages")
    assert "pdf.invisible_text" in {
        finding.code for finding in result["findings"] if finding.severity == "error"
    }

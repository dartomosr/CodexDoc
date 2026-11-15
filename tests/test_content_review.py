"""State-machine assertions are fixtures, never evidence of actual agent review."""

import json

import pytest
from docx import Document

from codexdoc.checks import validate_inputs
from codexdoc.cli import main
from codexdoc.content_review import CRITERIA, initialize, prepare, status, submit
from codexdoc.docx_engine import compose
from codexdoc.models import AgentProfile, Metadata, Source
from codexdoc.pipeline import _report
from codexdoc.storage import read_json, write_json
from codexdoc.workspaces import create_workspace, load_workspace


def baseline(workspace):
    initialize(workspace)
    write_json(
        workspace / "analysis/baseline.json",
        {
            "goal": "Проверка сохранения задачи",
            "scope": "Только клиентская часть",
            "items": [
                {
                    "id": "F-01",
                    "kind": "feature",
                    "text": "Интерактивная карта",
                    "basis": {"kind": "user", "locator": "Тестовое сообщение, не реальное согласование"},
                }
            ],
        },
    )


def assessment(workspace, stage):
    data = read_json(prepare(workspace, stage))["response_example"]
    data["reviewer"] = "TEST ONLY"
    data["summary"] = "TEST ONLY: проверка переходов состояний"
    data["criteria"] = dict.fromkeys(CRITERIA[stage], "TEST ONLY: структурная фикстура")
    data["coverage"][0].update(status="covered", block_ids=["intro-text"], note="TEST ONLY")
    path = workspace / "analysis/test-assessment.json"
    write_json(path, data)
    return path


def test_new_workspace_requires_review_and_has_contract_schemas(tmp_path, library):
    path = create_workspace(
        tmp_path, Metadata(), library.get_template("lab"), AgentProfile(), [], library=library
    )
    assert load_workspace(path)[0].content_review_required
    assert (path / "schemas/content-review.schema.json").is_file()
    assert status(path)["status"] == "invalid_baseline"


def test_stage_order_complete_review_and_text_invalidation(workspace):
    baseline(workspace)
    with pytest.raises(ValueError, match="plan"):
        prepare(workspace, "requirements")
    for stage in CRITERIA:
        result = submit(workspace, assessment(workspace, stage))
    assert result["status"] == "passed"
    document = read_json(workspace / "document.json")
    document["metadata"]["title"] += " изменён"
    write_json(workspace / "document.json", document)
    assert status(workspace)["status"] == "stale"
    with pytest.raises(ValueError, match="устарел"):
        submit(workspace, workspace / "analysis/test-assessment.json")


@pytest.mark.parametrize(
    "defect", ["omitted_item", "unknown_block", "empty_criterion", "no_block", "duplicate_item"]
)
def test_incomplete_attestations_rejected(workspace, defect):
    baseline(workspace)
    path = assessment(workspace, "plan")
    data = read_json(path)
    if defect == "omitted_item":
        data["coverage"] = []
    elif defect == "unknown_block":
        data["coverage"][0]["block_ids"] = ["nonexistent"]
    elif defect == "empty_criterion":
        data["criteria"]["scope"] = " "
    elif defect == "no_block":
        data["coverage"][0]["block_ids"] = []
    else:
        data["coverage"] *= 2
    write_json(path, data)
    with pytest.raises(ValueError):
        submit(workspace, path)


def test_missing_feature_or_error_prevents_passing(workspace):
    baseline(workspace)
    path = assessment(workspace, "plan")
    data = read_json(path)
    data["coverage"][0]["status"] = "missing"
    data["findings"] = [
        {
            "code": "scope.loss",
            "severity": "error",
            "message": "TEST ONLY: потеря карты; восстановить раздел",
            "block_id": "intro-text",
        }
    ]
    write_json(path, data)
    assert submit(workspace, path)["status"] == "issues"
    with pytest.raises(ValueError):
        prepare(workspace, "requirements")


def test_context_allowed_internally_format_rejected_and_bytes_invalidate(workspace):
    baseline(workspace)
    source_file = workspace / "assets/context.txt"
    source_file.write_text("Контекст", encoding="utf-8")
    manifest, _, _, _ = load_workspace(workspace)
    manifest.sources.append(Source(id="s001", name="Контекст", policy="context", path="assets/context.txt"))
    write_json(workspace / "workspace.json", manifest)
    data = read_json(workspace / "analysis/baseline.json")
    data["items"][0]["basis"] = {"kind": "source", "source_id": "s001", "locator": "Страница 1"}
    write_json(workspace / "analysis/baseline.json", data)
    submit(workspace, assessment(workspace, "plan"))
    source_file.write_text("Изменённый контекст", encoding="utf-8")
    assert status(workspace)["status"] == "stale"
    manifest.sources[0].policy = "format"
    write_json(workspace / "workspace.json", manifest)
    with pytest.raises(ValueError, match="основание"):
        prepare(workspace, "plan")


def test_ready_requires_semantic_review_even_after_visual_review(workspace):
    baseline(workspace)
    pdf_state = {"review_pages": [], "page_count": 1, "page_hashes": {}}
    report = _report(workspace, "fixture", {}, [], pdf_state, "adaptive")
    assert report.status == "review"
    assert report.technical_status == "passed"
    for stage in CRITERIA:
        submit(workspace, assessment(workspace, stage))
    assert _report(workspace, "fixture", {}, [], pdf_state, "adaptive").status == "ready"


def test_requirement_render_and_bibliography_editorial_checks(workspace):
    manifest, template, _, content = load_workspace(workspace)
    data = content.model_dump()
    data["blocks"].extend(
        [
            {
                "type": "requirement",
                "id": "FR-01",
                "text": "Карта сохраняет положение",
                "acceptance": "После открытия восстановлены координаты",
            },
            {
                "type": "requirement",
                "id": "FR-02",
                "text": "Карта меняет масштаб",
                "acceptance": "Масштаб изменяется по команде",
                "presentation": "table",
            },
            {"type": "paragraph", "id": "literal", "text": "Оставшаяся ссылка [s007]"},
            {"type": "heading", "id": "refs-manual", "text": "Список использованных источников"},
            {"type": "list", "id": "long-list", "items": ["Длинный пункт" * 20] * 13},
        ]
    )
    content = type(content).model_validate(data)
    codes = {f.code for f in validate_inputs(workspace, manifest, template, content)}
    assert {"citation.literal_id", "bibliography.manual", "editorial.long_list"} <= codes
    compose(workspace, content, template, [])
    doc = Document(workspace / ".cache/authored.docx")
    text = " ".join(p.text for p in doc.paragraphs)
    assert "FR-01" in text and "Критерий приёмки" in text
    assert any(
        "Карта меняет масштаб" in cell.text
        for table in doc.tables
        for row in table.rows
        for cell in row.cells
    )


def test_cli_status_is_read_only_and_init_preserves_document(workspace, capsys):
    previous = (workspace / "document.json").read_bytes()
    assert main(["content-review", str(workspace), "init"]) == 0
    capsys.readouterr()
    assert main(["content-review", str(workspace), "status"]) == 0
    assert json.loads(capsys.readouterr().out)["required"] is True
    assert (workspace / "document.json").read_bytes() == previous

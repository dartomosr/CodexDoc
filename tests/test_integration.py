import pymupdf
import pytest
from PIL import Image

from codexdoc.models import DocumentContent, Source
from codexdoc.pipeline import build, check, patch_block, publish_template, review
from codexdoc.storage import ROOT, read_json, write_json
from codexdoc.studio_state import current_build
from codexdoc.workspaces import load_workspace

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (ROOT / ".runtime/libreoffice/runtime.json").exists(), reason="Managed renderer is not installed"
    ),
]


def test_docx_pdf_toc_cache_stale_and_visual_gate(workspace):
    _, template, _, _ = load_workspace(workspace)
    template.toc = True
    write_json(workspace / "template.json", template)
    result = build(workspace)
    assert result.status == "review", [f.message for f in result.findings]
    assert result.page_count >= 4
    assert not [f for f in result.findings if f.severity == "error"]
    assert current_build(workspace)[1] == ""
    state = read_json(workspace / ".cache/build.json")
    with pymupdf.open(workspace / "output/document.pdf") as pdf:
        assert "Методика" in pdf[1].get_text()
    assert check(workspace).status == "review"
    assert build(workspace).artifact_hashes == result.artifact_hashes
    # Simulate explicit reviewer attestations to test the gate; this test is not a human visual review.
    for page in result.review_pages:
        reviewed = review(workspace, page, "TEST ONLY: review state-machine fixture")
    assert reviewed.status == "ready"
    assert current_build(workspace)[1] == ""
    reviews_path = workspace / ".cache/reviews.json"
    saved_reviews = read_json(reviews_path)
    write_json(reviews_path, {})
    assert "Просмотр" in current_build(workspace)[1]
    write_json(reviews_path, saved_reviews)
    patch_block(
        workspace,
        "intro-text",
        "Это изменённый вводный абзац, который используется для проверки устаревания результатов и адресной пересборки документа.",
    )
    assert check(workspace).status == "blocked"
    new = build(workspace)
    assert not [f for f in new.findings if f.severity == "error"]
    current = read_json(workspace / ".cache/build.json")
    assert current["composition"]["incremental"]
    assert current["composition"]["changed_blocks"] == ["intro-text"]
    assert current["fingerprint"] != state["fingerprint"]
    # Altering final output is never accepted as a checked current artifact.
    with (workspace / "output/document.docx").open("ab") as stream:
        stream.write(b"tampered")
    assert "изменены" in current_build(workspace)[1]
    assert "build.modified" in {f.code for f in check(workspace).findings}


def test_template_publish_requires_review_and_resolution(workspace, library):
    manifest, template, _, _ = load_workspace(workspace)
    manifest.mode = "template"
    template.id = "approved-test"
    template.review_note = "TEST ONLY: verified rule contract for the test fixture"
    write_json(workspace / "workspace.json", manifest)
    write_json(workspace / "template.json", template)
    result = build(workspace)
    assert result.status == "review", [f.message for f in result.findings]
    with pytest.raises(ValueError):
        publish_template(workspace, library)
    for page in result.review_pages:
        review(workspace, page, "TEST ONLY: publication gate fixture")
    published = publish_template(workspace, library)
    assert published.is_file()
    assert library.get_template("approved-test").review_note


def test_draft_preview_cannot_be_marked_ready_or_published(workspace, library):
    manifest, template, _, _ = load_workspace(workspace)
    manifest.mode = "template"
    template.id = "draft-preview-test"
    template.engine_version = "0.1.0"
    template.status = "draft"
    template.unresolved = ["TEST ONLY: approval still needed"]
    template.review_note = "TEST ONLY: preview fixture"
    write_json(workspace / "workspace.json", manifest)
    write_json(workspace / "template.json", template)

    result = build(workspace, preview=True)

    assert result.status == "preview"
    assert result.page_count > 0
    assert check(workspace).status == "preview"
    with pytest.raises(ValueError, match="предпросмотра черновика"):
        review(workspace, result.review_pages[0], "TEST ONLY: should not be accepted")
    with pytest.raises(ValueError):
        publish_template(workspace, library)


def test_images_multipage_tables_and_references(workspace):
    manifest, template, _, content = load_workspace(workspace)
    image = workspace / "assets" / "sample.png"
    Image.new("RGB", (600, 200), color=(52, 88, 132)).save(image)
    manifest.sources.append(
        Source(
            id="test-source",
            name="Проверочная запись",
            reference="Предоставленная проверочная запись, 2026.",
            reference_verified=True,
        )
    )
    data = content.model_dump()
    position = next(i for i, block in enumerate(data["blocks"]) if block["type"] == "appendix")
    data["blocks"][position:position] = [
        {
            "type": "requirement",
            "id": "FR-MAP-01",
            "text": "Интерактивная карта сохраняет положение пользователя.",
            "acceptance": "При повторном открытии восстановлены координаты карты.",
            "presentation": "table",
        },
        {
            "type": "paragraph",
            "id": "citation-test",
            "text": "Ссылка служит для проверки механизма библиографических записей.",
            "citations": ["test-source"],
        },
        {
            "type": "image",
            "id": "image-test",
            "path": "assets/sample.png",
            "caption": "Проверочное изображение",
            "width_mm": 110,
        },
        {
            "type": "table",
            "id": "long-table",
            "caption": "Повторяющиеся строки",
            "rows": [["Номер", "Описание"], *[[str(i), f"Строка проверки {i}"] for i in range(60)]],
        },
    ]
    write_json(workspace / "workspace.json", manifest)
    write_json(workspace / "document.json", DocumentContent.model_validate(data))
    result = build(workspace)
    assert result.status == "review", [f.message for f in result.findings]
    with pymupdf.open(workspace / "output/document.pdf") as pdf:
        text = "\n".join(page.get_text() for page in pdf)
        assert "Строка проверки 59" in text
        assert "Предоставленная проверочная запись" in text
        assert text.index("СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ") < text.index("ПРИЛОЖЕНИЕ А")
        assert any(page.get_images() for page in pdf)
        assert "FR-MAP-01" in text
        assert "координаты карты" in text
    assert (workspace / "output/overview.png").is_file()

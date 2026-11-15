import pytest
from docx import Document
from pydantic import ValidationError

from codexdoc.docx_engine import compose, contexts
from codexdoc.models import (
    BibliographyFormat,
    ContentsFormat,
    Metadata,
    ParagraphStyleSpec,
    Signatory,
    Source,
    Template,
    TitleBlock,
    TitleLayout,
)
from codexdoc.storage import write_json
from codexdoc.workspaces import load_workspace


def configured(template):
    template.title_layout = TitleLayout()
    template.contents_format = ContentsFormat(
        entries=ParagraphStyleSpec(size_pt=12, line_spacing=1, left_mm=2), level_indent_mm=6
    )
    template.bibliography_format = BibliographyFormat(
        title="СПИСОК ЛИТЕРАТУРЫ",
        entries=ParagraphStyleSpec(size_pt=12, line_spacing=1, left_mm=7, first_line_mm=-7),
        heading_numbered=True,
        numbering="brackets",
    )
    template.structural_numbered = True
    template.toc = True
    return template


def test_old_template_defaults_preserve_legacy_layout():
    template = Template.model_validate({"id": "old", "name": "Старый", "title_page": True, "toc": True})
    assert template.title_layout is None
    assert template.contents_format is None
    assert template.bibliography_format is None
    assert not template.structural_numbered
    assert Metadata(author="Иванов").authors == []


def test_title_layout_rejects_overlap_and_unknown_substitutions():
    with pytest.raises(ValidationError, match="пересечений"):
        TitleLayout(blocks=[TitleBlock(y_mm=0, height_mm=20), TitleBlock(y_mm=10)])
    with pytest.raises(ValidationError, match="подстановка"):
        TitleBlock(text="{title.__class__}")
    with pytest.raises(ValidationError, match="выходят за страницу"):
        Template(id="bad", name="Bad", title_layout=TitleLayout(blocks=[TitleBlock(y_mm=230, height_mm=20)]))


def test_composition_has_signature_rows_and_custom_references(workspace):
    manifest, template, _, content = load_workspace(workspace)
    configured(template)
    content.metadata.authors = [Signatory(name="Иванов И.И."), Signatory(name="Петров П.П.")]
    source = Source(
        id="ref", name="Fixture", reference="Контрольная библиографическая запись", reference_verified=True
    )
    manifest.sources = [source]
    for b in content.blocks:
        if b.type == "paragraph":
            b.citations = ["ref"]
            break
    compose(workspace, content, template, manifest.sources)
    doc = Document(workspace / ".cache/authored.docx")
    xml = doc.element.xml
    assert "Иванов И.И." in xml and "Петров П.П." in xml
    assert "____________" in xml
    assert "4 СПИСОК ЛИТЕРАТУРЫ" in xml
    assert "[1] Контрольная" in xml
    assert contexts(content, template)[0]["intro"] == "1 ВВЕДЕНИЕ"


@pytest.mark.integration
def test_layout_survives_actual_docx_pdf_export(workspace):
    import pymupdf

    from codexdoc.pipeline import build

    manifest, template, _, content = load_workspace(workspace)
    configured(template)
    content.metadata.authors = [
        Signatory(name="Иванов И.И.", group="А-101"),
        Signatory(name="Петров П.П.", group="А-101"),
    ]
    content.metadata.reviewers = [Signatory(name="Сидоров С.С.", role="Проверил")]
    source = Source(
        id="ref",
        name="Fixture",
        reference="Контрольная библиографическая запись для проверки интервалов и выступа первой строки. Это тестовая запись.",
        reference_verified=True,
    )
    manifest.sources = [source]
    for block in content.blocks:
        if block.type == "paragraph":
            block.citations = ["ref"]
            break
    write_json(workspace / "workspace.json", manifest)
    write_json(workspace / "template.json", template)
    write_json(workspace / "document.json", content)
    report = build(workspace)
    assert not [f for f in report.findings if f.severity == "error"], report.model_dump()
    with pymupdf.open(workspace / "output/document.pdf") as pdf:
        first = pdf[0]
        assert "Петров" in first.get_text()
        bottom = first.search_for("Москва 2026")
        assert bottom and bottom[0].y0 > first.rect.height * 0.85
        assert "СОДЕРЖАНИЕ" in pdf[1].get_text()
        entries = [
            s
            for b in pdf[1].get_text("dict")["blocks"]
            for line in b.get("lines", [])
            for s in line["spans"]
            if "ВВЕДЕНИЕ" in s["text"]
        ]
        assert entries and abs(entries[0]["size"] - 12) < 0.2
        lines = [line for b in first.get_text("dict")["blocks"] for line in b.get("lines", [])]
        name = next(s for line in lines for s in line["spans"] if "Иванов" in s["text"])
        signatures = [s for line in lines for s in line["spans"] if "____" in s["text"]]
        assert any(abs(s["origin"][1] - name["origin"][1]) < 1 for s in signatures)

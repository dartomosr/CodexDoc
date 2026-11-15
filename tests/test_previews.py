import pymupdf
import pytest

from codexdoc.models import AgentProfile, Metadata, Source
from codexdoc.previews import render_source_pages
from codexdoc.workspaces import create_workspace


def test_source_pdf_pages_render_locally_to_analysis_previews(tmp_path, library):
    pdf = pymupdf.open()
    pdf.new_page(width=595, height=842)
    pdf.new_page(width=595, height=842)
    original = tmp_path / "source.pdf"
    pdf.save(original)
    pdf.close()
    workspace = create_workspace(
        tmp_path / "workspaces",
        Metadata(title="PDF preview"),
        library.get_template("report"),
        AgentProfile(),
        [(original, Source(id="pending", name=original.name, policy="context"))],
        library=library,
    )

    rendered = render_source_pages(workspace, "s001", [2])

    assert rendered == [workspace / "analysis/previews/s001/page-2.png"]
    assert rendered[0].is_file()
    with pymupdf.open(rendered[0]) as image:
        assert image.page_count == 1


def test_source_preview_rejects_excluded_source(tmp_path, library):
    original = tmp_path / "excluded.pdf"
    pdf = pymupdf.open()
    pdf.new_page()
    pdf.save(original)
    pdf.close()
    workspace = create_workspace(
        tmp_path / "workspaces",
        Metadata(title="Excluded PDF"),
        library.get_template("report"),
        AgentProfile(),
        [(original, Source(id="pending", name=original.name, policy="exclude"))],
        library=library,
    )

    with pytest.raises(ValueError, match="запрещён"):
        render_source_pages(workspace, "s001", [1])

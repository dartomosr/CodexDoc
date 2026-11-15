"""Declarative title layout and reusable paragraph formatting.

Borderless tables keep signatures editable and aligned in Word and LibreOffice.
Minimum row heights never clip text; PDF validation rejects shifted/overflowed blocks.
"""

from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_ROW_HEIGHT_RULE, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor

from codexdoc.models import Metadata, ParagraphStyleSpec, Signatory, Template

ALIGNMENTS = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
}


def paragraph_style(paragraph, spec: ParagraphStyleSpec, default_font: str):
    pf = paragraph.paragraph_format
    pf.alignment = ALIGNMENTS[spec.alignment]
    pf.line_spacing = spec.line_spacing
    pf.first_line_indent = Mm(spec.first_line_mm)
    pf.left_indent = Mm(spec.left_mm)
    pf.right_indent = Mm(spec.right_mm)
    pf.space_before = Pt(spec.before_pt)
    pf.space_after = Pt(spec.after_pt)
    for run in paragraph.runs:
        run.font.name = spec.font or default_font
        run.font.size = Pt(spec.size_pt)
        run.font.bold = spec.bold
        run.font.color.rgb = RGBColor(0, 0, 0)


def title_text(text: str, meta: Metadata) -> str:
    return "\n".join(line.strip() for line in text.format_map(meta.model_dump()).splitlines() if line.strip())


def people(meta: Metadata, kind: str) -> list[Signatory]:
    if kind == "authors":
        return meta.authors or ([Signatory(name=meta.author, group=meta.group)] if meta.author else [])
    return meta.reviewers or ([Signatory(name=meta.supervisor, role="Проверил")] if meta.supervisor else [])


def clear_table(table):
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    properties = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement("w:" + edge)
        element.set(qn("w:val"), "nil")
        borders.append(element)
    properties.append(borders)
    margins = OxmlElement("w:tblCellMar")
    for edge in ("top", "left", "bottom", "right"):
        element = OxmlElement("w:" + edge)
        element.set(qn("w:w"), "0")
        element.set(qn("w:type"), "dxa")
        margins.append(element)
    properties.append(margins)


def compact(paragraph):
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.line_spacing = Pt(1)
    paragraph.paragraph_format.first_line_indent = Mm(0)
    paragraph.add_run().font.size = Pt(1)


def signature_table(cell, signers: list[Signatory], spec: ParagraphStyleSpec, font: str, width: float):
    compact(cell.paragraphs[0])
    if not signers:
        return
    table = cell.add_table(rows=0, cols=3)
    clear_table(table)
    widths = [width * 0.28, width * 0.24, width * 0.48]
    for column, size in zip(table.columns, widths, strict=True):
        column.width = Mm(size)
    for signer in signers:
        cells = table.add_row().cells
        for target, text, size in zip(
            cells, [signer.role + ":", "____________", signer.name], widths, strict=True
        ):
            target.width = Mm(size)
            target.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.BOTTOM
            p = target.paragraphs[0]
            p.text = text
            paragraph_style(p, spec.model_copy(update={"alignment": "left", "first_line_mm": 0}), font)
        if signer.group:
            cells = table.add_row().cells
            for target in cells:
                compact(target.paragraphs[0])
            cells[2].paragraphs[0].text = "Группа: " + signer.group
            paragraph_style(cells[2].paragraphs[0], spec.model_copy(update={"alignment": "left"}), font)
    compact(cell.paragraphs[-1])


def render_title(document, meta: Metadata, template: Template):
    layout = template.title_layout
    assert layout is not None
    width = template.page.width_mm - template.page.left_mm - template.page.right_mm
    available = template.page.height_mm - template.page.top_mm - template.page.bottom_mm - 3
    table = document.add_table(rows=0, cols=1)
    clear_table(table)
    table.columns[0].width = Mm(width)

    def row(height, bottom=False):
        item = table.add_row()
        item.height = Mm(height)
        item.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
        item._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
        cell = item.cells[0]
        cell.width = Mm(width)
        cell.vertical_alignment = (
            WD_CELL_VERTICAL_ALIGNMENT.BOTTOM if bottom else WD_CELL_VERTICAL_ALIGNMENT.TOP
        )
        compact(cell.paragraphs[0])
        return cell

    cursor = 0.0
    for block in layout.blocks:
        if block.y_mm > cursor + 0.01:
            row(block.y_mm - cursor)
        cell = row(block.height_mm)
        if block.kind == "text":
            p = cell.paragraphs[0]
            p.text = title_text(block.text, meta)
            paragraph_style(p, block.style, template.text.font)
        else:
            signature_table(cell, people(meta, block.kind), block.style, template.text.font, width)
        cursor = block.y_mm + block.height_mm
    bottom_y = available - layout.bottom_height_mm
    if bottom_y > cursor + 0.01:
        row(bottom_y - cursor)
    cell = row(layout.bottom_height_mm, bottom=True)
    p = cell.paragraphs[0]
    p.text = title_text(layout.bottom_text, meta)
    paragraph_style(p, layout.bottom_style, template.text.font)
    # A minimal paragraph after a table is needed by Word; the budget above reserves its height.
    paragraph = document.add_paragraph()
    compact(paragraph)
    paragraph.add_run().add_break(WD_BREAK.PAGE)

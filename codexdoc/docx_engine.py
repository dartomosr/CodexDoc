"""DOCX-first composition with persistent block spans for targeted updates."""

from copy import deepcopy
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor

from codexdoc.layout import paragraph_style, render_title
from codexdoc.models import DocumentContent, Source, Table, Template
from codexdoc.storage import contained, digest, object_digest, read_json, write_json


def field(paragraph, instruction: str, display: str = ""):
    run = paragraph.add_run()
    start = OxmlElement("w:fldChar")
    start.set(qn("w:fldCharType"), "begin")
    code = OxmlElement("w:instrText")
    code.set(qn("xml:space"), "preserve")
    code.text = " " + instruction + " "
    separator = OxmlElement("w:fldChar")
    separator.set(qn("w:fldCharType"), "separate")
    run._r.extend([start, code, separator])
    if display:
        paragraph.add_run(display)
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    paragraph.add_run()._r.append(end)


def set_font(style, name: str, size: float, bold: bool = False):
    style.font.name = name
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.color.rgb = RGBColor(0, 0, 0)
    fonts = style.element.get_or_add_rPr().get_or_add_rFonts()
    for attribute in ("ascii", "hAnsi", "cs", "eastAsia"):
        fonts.set(qn("w:" + attribute), name)
    for attribute in ("asciiTheme", "hAnsiTheme", "cstheme", "eastAsiaTheme"):
        fonts.attrib.pop(qn("w:" + attribute), None)


def configure(document, template: Template):
    section = document.sections[0]
    for name, value in template.page.model_dump().items():
        attr = {"width_mm": "page_width", "height_mm": "page_height"}.get(
            name, name.replace("_mm", "_margin")
        )
        setattr(section, attr, Mm(value))
    section.header_distance = Mm(10)
    section.footer_distance = Mm(10)
    section.different_first_page_header_footer = template.title_page
    if template.title_page:
        # Materialize empty first-page parts: an absent reference can inherit the numbered footer in LO.
        section.first_page_footer.paragraphs[0].text = ""
        section.first_page_header.paragraphs[0].text = ""
    for style in document.styles:
        for border in list(style.element.iter(qn("w:pBdr"))):
            border.getparent().remove(border)
    normal = document.styles["Normal"]
    set_font(normal, template.text.font, template.text.size_pt)
    pf = normal.paragraph_format
    pf.first_line_indent = Mm(template.text.indent_mm)
    pf.line_spacing = template.text.line_spacing
    pf.space_after = Pt(template.text.paragraph_after_pt)
    pf.space_before = Pt(0)
    pf.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    pf.widow_control = True
    for name in (
        "Title",
        "Subtitle",
        "Caption",
        "Header",
        "Footer",
        "TOC Heading",
        "Heading 1",
        "Heading 2",
        "Heading 3",
        "List Paragraph",
    ):
        style = document.styles[name]
        set_font(
            style,
            template.text.font,
            template.text.size_pt,
            name.startswith("Heading") and template.heading_bold,
        )
        style.paragraph_format.first_line_indent = Mm(
            template.text.indent_mm if name.startswith("Heading") else 0
        )
        style.paragraph_format.space_after = Pt(6 if name.startswith("Heading") else 0)
        style.paragraph_format.space_before = Pt(12 if name.startswith("Heading") else 0)
        style.paragraph_format.line_spacing = template.text.line_spacing
        style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
        style.paragraph_format.keep_with_next = name.startswith("Heading") or name == "TOC Heading"
        style.paragraph_format.page_break_before = name == "Heading 1" and template.chapter_new_page
    document.styles["Title"].paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    document.styles["TOC Heading"].paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    document.styles["Caption"].font.italic = False
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if template.footer:
        footer.add_run(template.footer + " ")
    field(footer, "PAGE", "1")
    section.header.paragraphs[0].text = template.header
    settings = document.settings.element
    update = OxmlElement("w:updateFields")
    update.set(qn("w:val"), "true")
    settings.append(update)
    document.core_properties.author = ""
    document.core_properties.last_modified_by = ""


def add_front(document, content: DocumentContent, template: Template):
    meta = content.metadata
    document.core_properties.title = meta.title
    if template.title_page and template.title_layout:
        render_title(document, meta, template)
    elif template.title_page:
        for line in (meta.organization, meta.department):
            if line:
                p = document.add_paragraph(line)
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.first_line_indent = Mm(0)
        p = document.add_paragraph(meta.kind.upper())
        p.paragraph_format.space_before = Pt(90)
        p.paragraph_format.first_line_indent = Mm(0)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        document.add_paragraph(meta.title, "Title")
        for line in (
            f"Выполнил: {meta.author}" if meta.author else "",
            f"Группа: {meta.group}" if meta.group else "",
            f"Проверил: {meta.supervisor}" if meta.supervisor else "",
        ):
            if line:
                p = document.add_paragraph(line)
                p.paragraph_format.first_line_indent = Mm(0)
                p.paragraph_format.space_before = Pt(12)
                p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        if meta.city or meta.year:
            p = document.add_paragraph(" ".join(x for x in [meta.city, meta.year] if x))
            p.paragraph_format.space_before = Pt(90)
            p.paragraph_format.first_line_indent = Mm(0)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        document.add_page_break()
    else:
        document.add_paragraph(meta.title, "Title")
    if template.toc:
        settings = template.contents_format
        heading = document.add_paragraph(settings.title if settings else "СОДЕРЖАНИЕ", "TOC Heading")
        if settings:
            paragraph_style(heading, settings.heading, template.text.font)
        p = document.add_paragraph()
        start = OxmlElement("w:bookmarkStart")
        start.set(qn("w:id"), "900000")
        start.set(qn("w:name"), "CodexDocTOC")
        end = OxmlElement("w:bookmarkEnd")
        end.set(qn("w:id"), "900000")
        p._p.extend([start, end])
        if not template.chapter_new_page:
            document.add_page_break()


def contexts(content: DocumentContent, template: Template):
    counters = [0, 0, 0]
    figures = tables = equations = appendices = 0
    refs = {}
    result = {}
    for block in content.blocks:
        label = ""
        if block.type == "heading":
            numbered = block.numbered or (block.level == 1 and template.structural_numbered)
            if template.numbered_headings and numbered:
                counters[block.level - 1] += 1
                for i in range(block.level, 3):
                    counters[i] = 0
                label = ".".join(str(n) for n in counters[: block.level]) + " "
            label += block.text.upper() if block.level == 1 and not block.numbered else block.text
        elif block.type == "table":
            tables += 1
            label = f"Таблица {tables} – {block.caption}"
        elif block.type == "image":
            figures += 1
            label = f"Рисунок {figures} – {block.caption}"
        elif block.type == "equation" and block.number:
            equations += 1
            label = f"({equations})"
        elif block.type == "appendix":
            alphabet = "АБВГДЕЖИКЛМНПРСТУФХЦШЩЭЮЯ"
            label = "ПРИЛОЖЕНИЕ " + (
                alphabet[appendices] if appendices < len(alphabet) else str(appendices + 1)
            )
            appendices += 1
        elif block.type == "paragraph":
            for source_id in block.citations:
                refs.setdefault(source_id, len(refs) + 1)
        result[block.id] = label
    return result, refs


def append_block(document, block, label: str, refs: dict, template: Template, workspace: Path):
    if block.type == "requirement":
        if block.presentation == "table":
            append_block(
                document,
                Table(
                    id=block.id,
                    caption=block.id,
                    header=False,
                    rows=[
                        ["Требование", block.text],
                        ["Критерий приёмки", block.acceptance],
                        [
                            "Подсистема / приоритет",
                            " / ".join(v for v in [block.subsystem, block.priority] if v),
                        ],
                    ],
                ),
                block.id,
                refs,
                template,
                workspace,
            )
        else:
            p = document.add_paragraph()
            p.add_run(block.id + ". ").bold = True
            p.add_run(block.text)
            p.paragraph_format.keep_with_next = True
            p = document.add_paragraph("Критерий приёмки: " + block.acceptance)
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            if block.subsystem or block.priority:
                document.add_paragraph(" / ".join(v for v in [block.subsystem, block.priority] if v))
    elif block.type == "heading":
        p = document.add_paragraph(label, f"Heading {block.level}")
        if block.level == 1 and not block.numbered:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.first_line_indent = Mm(0)
            if template.structural_heading_style:
                paragraph_style(p, template.structural_heading_style, template.text.font)
    elif block.type == "paragraph":
        p = document.add_paragraph(block.text)
        if block.citations:
            p.add_run(" [" + ", ".join(str(refs[s]) for s in block.citations) + "]")
    elif block.type == "table":
        caption = document.add_paragraph(label, "Caption")
        caption.paragraph_format.keep_with_next = True
        table = document.add_table(rows=0, cols=len(block.rows[0]))
        table.style = "Table Grid"
        table.autofit = False
        width = (template.page.width_mm - template.page.left_mm - template.page.right_mm) / len(block.rows[0])
        for col in table.columns:
            col.width = Mm(width)
        for i, row in enumerate(block.rows):
            cells = table.add_row().cells
            for cell, text in zip(cells, row, strict=True):
                cell.width = Mm(width)
                cell.text = text
                for p in cell.paragraphs:
                    p.paragraph_format.first_line_indent = Mm(0)
                    p.paragraph_format.line_spacing = 1
                    p.paragraph_format.space_after = Pt(4)
                    p.paragraph_format.space_before = Pt(4)
                    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                    for run in p.runs:
                        run.font.size = Pt(min(12, template.text.size_pt))
                        run.bold = i == 0 and block.header
            row_element = table.rows[-1]._tr
            properties = row_element.get_or_add_trPr()
            properties.append(OxmlElement("w:cantSplit"))
            if i == 0 and block.header:
                properties.append(OxmlElement("w:tblHeader"))
        document.add_paragraph().paragraph_format.space_after = Pt(0)
    elif block.type == "image":
        p = document.add_paragraph()
        p.paragraph_format.first_line_indent = Mm(0)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.keep_with_next = True
        p.add_run().add_picture(str(contained(workspace, block.path)), width=Mm(block.width_mm))
        p = document.add_paragraph(label, "Caption")
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif block.type == "equation":
        p = document.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = Mm(0)
        math = OxmlElement("m:oMath")
        run = OxmlElement("m:r")
        properties = OxmlElement("w:rPr")
        color = OxmlElement("w:color")
        color.set(qn("w:val"), "000000")
        properties.append(color)
        run.append(properties)
        text = OxmlElement("m:t")
        text.text = block.text
        run.append(text)
        math.append(run)
        p._p.append(math)
        if label:
            p.add_run("    " + label)
    elif block.type == "list":
        for i, item in enumerate(block.items, 1):
            p = document.add_paragraph(f"{i}) {item}" if block.ordered else f"• {item}")
            p.paragraph_format.left_indent = Mm(template.text.indent_mm)
            p.paragraph_format.first_line_indent = Mm(-5)
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    elif block.type == "page_break":
        document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    elif block.type == "appendix":
        p = document.add_paragraph(label, "Heading 1")
        p.paragraph_format.page_break_before = True
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p = document.add_paragraph(block.text)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = Mm(0)
        p.paragraph_format.keep_with_next = True


def compose(workspace: Path, content: DocumentContent, template: Template, sources: list[Source]) -> dict:
    cache = workspace / ".cache"
    cache.mkdir(exist_ok=True)
    path = cache / "authored.docx"
    state_path = cache / "composition.json"
    labels, refs = contexts(content, template)
    base = object_digest(
        {
            "metadata": content.metadata.model_dump(),
            "template": template.model_dump(),
            "sources": [s.model_dump() for s in sources],
            "references": refs,
            "ids": [b.id for b in content.blocks],
            "composer": 6,
        }
    )
    hashes = {
        b.id: object_digest(
            {
                "block": b.model_dump(),
                "label": labels[b.id],
                "asset": digest(contained(workspace, b.path)) if b.type == "image" else "",
            }
        )
        for b in content.blocks
    }
    old = read_json(state_path) if state_path.exists() else {}
    incremental = old.get("base") == base and path.exists() and old.get("authored_hash") == digest(path)
    spans = {}
    changed = []
    if incremental:
        document = Document(str(path))
        # Reverse order preserves original indexes while replacing spans.
        for block in reversed(content.blocks):
            if old["hashes"].get(block.id) == hashes[block.id]:
                continue
            changed.append(block.id)
            begin, end = old["spans"][block.id]
            old_nodes = list(document.element.body)[begin:end]
            before = len(document.element.body) - 1
            append_block(document, block, labels[block.id], refs, template, workspace)
            nodes = list(document.element.body)[before:-1]
            for node in nodes:
                document.element.body.remove(node)
            for node in old_nodes:
                document.element.body.remove(node)
            for index, node in enumerate(nodes):
                document.element.body.insert(begin + index, node)
            delta = len(nodes) - (end - begin)
            for pair in old["spans"].values():
                if pair[0] > begin:
                    pair[0] += delta
                    pair[1] += delta
            old["spans"][block.id] = [begin, begin + len(nodes)]
        spans = old["spans"]
    else:
        document = Document()
        configure(document, template)
        add_front(document, content, template)
        references_written = False
        for block in content.blocks:
            if block.type == "appendix" and refs and not references_written:
                add_references(document, sources, refs, template, content)
                references_written = True
            start = len(document.element.body) - 1
            append_block(document, block, labels[block.id], refs, template, workspace)
            spans[block.id] = [start, len(document.element.body) - 1]
            changed.append(block.id)
        if refs and not references_written:
            add_references(document, sources, refs, template, content)
    temporary = cache / "authored.next.docx"
    document.save(str(temporary))
    temporary.replace(path)
    state = {
        "base": base,
        "hashes": hashes,
        "spans": deepcopy(spans),
        "authored_hash": digest(path),
        "incremental": incremental,
        "changed_blocks": list(reversed(changed)) if incremental else changed,
    }
    write_json(state_path, state)
    write_json(
        path.with_suffix(".render.json"),
        {
            "contents_format": template.contents_format.model_dump() if template.contents_format else None,
            "font": template.text.font,
            "width_mm": template.page.width_mm - template.page.left_mm - template.page.right_mm,
        },
    )
    return state


def add_references(document, sources: list[Source], refs: dict, template: Template, content: DocumentContent):
    settings = template.bibliography_format
    title = settings.title if settings else "СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ"
    if settings and settings.heading_numbered:
        preceding = []
        for block in content.blocks:
            if block.type == "appendix":
                break
            preceding.append(block)
        count = sum(
            b.type == "heading"
            and b.level == 1
            and (b.numbered or template.structural_numbered)
            and template.numbered_headings
            for b in preceding
        )
        title = f"{count + 1} {title}"
    p = document.add_paragraph(title, "Heading 1" if not settings or settings.include_in_toc else "Normal")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Mm(0)
    if settings:
        paragraph_style(p, settings.heading, template.text.font)
        p.paragraph_format.page_break_before = settings.new_page
        p.paragraph_format.keep_with_next = True
    by_id = {s.id: s for s in sources}
    for source_id, number in refs.items():
        prefix = (
            f"[{number}] "
            if settings and settings.numbering == "brackets"
            else ""
            if settings and settings.numbering == "none"
            else f"{number}. "
        )
        p = document.add_paragraph(prefix + by_id[source_id].reference)
        if settings:
            paragraph_style(p, settings.entries, template.text.font)

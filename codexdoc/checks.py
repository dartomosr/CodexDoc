"""Layered validation with explicit limits: schemas, OOXML, rendered geometry."""

import re
from pathlib import Path

import pymupdf
from docx import Document
from docx.oxml.ns import qn
from docx.styles.style import ParagraphStyle

from codexdoc import __version__
from codexdoc.layout import people, title_text
from codexdoc.models import DocumentContent, Finding, Template, Workspace
from codexdoc.storage import contained, digest


def validate_inputs(
    workspace: Path,
    manifest: Workspace,
    template: Template,
    content: DocumentContent,
    allow_template_draft: bool = False,
) -> list[Finding]:
    findings = []

    def error(code, message, block=""):
        findings.append(Finding(code=code, message=message, block_id=block))

    if not allow_template_draft and (template.status != "ready" or template.unresolved):
        error(
            "template.unresolved",
            "Шаблон не подтверждён: " + "; ".join(template.unresolved or ["status=draft"]),
        )
    if not allow_template_draft and template.engine_version != __version__:
        error(
            "template.engine_outdated",
            f"Шаблон создан или проверен движком {template.engine_version or 'неизвестной версии'}, текущий движок {__version__}. Повторно проверьте и опубликуйте шаблон.",
        )
    if not content.blocks:
        error("content.empty", "Документ пока не содержит блоков. Агент должен заполнить document.json.")
    if (
        template.title_page
        and not template.title_layout
        and (content.metadata.authors or content.metadata.reviewers)
    ):
        error(
            "title.layout_required",
            "Для списков авторов и проверяющих включите конструктор титула в шаблоне.",
        )
    if not any(b.type == "paragraph" for b in content.blocks):
        error("content.body_missing", "Отсутствует основной текст документа.")
    sources = {s.id: s for s in manifest.sources}
    if len(sources) != len(manifest.sources):
        error("source.duplicate_id", "Идентификаторы источников должны быть уникальными.")
    for source in manifest.sources:
        if source.path:
            try:
                path = contained(workspace, source.path)
                if not path.is_file() or digest(path) != source.sha256:
                    error(
                        "source.changed",
                        f"Изменился или отсутствует оригинал {source.id}: {source.name}. Переподготовьте источник и его анализ.",
                    )
            except ValueError as exc:
                error("source.path", str(exc))
    for provenance in template.provenance:
        if provenance.sha256 and provenance.location:
            try:
                path = contained(workspace, provenance.location)
                if not path.is_file() or digest(path) != provenance.sha256:
                    error(
                        "norm.changed",
                        f"Нормативный источник/образец изменён или отсутствует: {provenance.title}",
                    )
            except ValueError as exc:
                error("norm.path", str(exc))
    tracked_norms = set()
    for provenance in template.provenance:
        if not provenance.location:
            continue
        try:
            relative = contained(workspace, provenance.location).relative_to(workspace).as_posix()
        except ValueError:
            continue
        if relative.startswith("norms/"):
            tracked_norms.add(relative.casefold())
    norm_directory = workspace / "norms"
    if norm_directory.is_dir():
        for norm_file in sorted(path for path in norm_directory.rglob("*") if path.is_file()):
            relative = norm_file.relative_to(workspace).as_posix()
            if relative.casefold() not in tracked_norms:
                error(
                    "norm.untracked",
                    f"Нормативный файл не включён в provenance шаблона: {relative}. Проверьте его роль и целостность.",
                )
    headings = {b.text.casefold().strip() for b in content.blocks if b.type == "heading"}
    for title in template.required_sections:
        if title.casefold().strip() not in headings:
            error("section.missing", f"Отсутствует обязательный раздел: {title}")
    previous_level = 0
    used_sources = {ref for b in content.blocks if b.type == "paragraph" for ref in b.citations}
    for source in manifest.sources:
        if source.role == "citable" and source.id not in used_sources:
            findings.append(
                Finding(
                    code="citation.unused",
                    severity="info",
                    message=f"Источник {source.id} не цитируется; включать каждый приложенный материал не обязательно.",
                )
            )
    numbered_levels = set()
    for block in content.blocks:
        if block.type == "heading":
            if block.level > previous_level + 1:
                error("heading.jump", "Пропущен уровень заголовка.", block.id)
            previous_level = block.level
            if block.numbered:
                if block.level > 1 and block.level - 1 not in numbered_levels:
                    error(
                        "heading.numbering",
                        "Для нумерованного подраздела отсутствует нумерованный родитель.",
                        block.id,
                    )
                numbered_levels = {level for level in numbered_levels if level < block.level} | {block.level}
        if block.type == "paragraph":
            for ref in block.citations:
                source = sources.get(ref)
                if source is None:
                    error("citation.unknown", f"Неизвестный источник: {ref}", block.id)
                elif source.role != "citable":
                    error(
                        "citation.forbidden",
                        f"Для {ref} запрещены ссылки: роль {source.role}.",
                        block.id,
                    )
                elif not source.reference.strip() or not source.reference_verified:
                    error(
                        "citation.unverified",
                        f"Для {ref} не подтверждена библиографическая запись.",
                        block.id,
                    )
        if block.type == "image":
            try:
                path = contained(workspace, block.path)
                if not path.is_file():
                    error("image.missing", f"Изображение не найдено: {block.path}", block.id)
            except ValueError as exc:
                error("image.path", str(exc), block.id)
            if block.width_mm > template.page.width_mm - template.page.left_mm - template.page.right_mm + 0.1:
                error("image.width", "Изображение шире области текста.", block.id)
        serialized_text = " ".join(_block_texts(block))
        if re.search(r"\[\s*s\d+\s*\]", serialized_text, re.IGNORECASE) or any(
            f"[{source_id}]" in serialized_text for source_id in sources
        ):
            error(
                "citation.literal_id",
                "Остался технический ID источника. Используйте citations; номера создаёт движок.",
                block.id,
            )
        if block.type == "heading" and re.sub(r"[\s\d.]+", " ", block.text.casefold()).strip() in {
            "список использованных источников",
            "список литературы",
            "библиография",
            "references",
        }:
            findings.append(
                Finding(
                    code="bibliography.manual",
                    severity="warning",
                    block_id=block.id,
                    message="Возможен ручной дубль библиографии. Список создаётся автоматически по citations; проверьте этот раздел.",
                )
            )
        if block.type == "list" and (len(block.items) > 12 or sum(len(item) for item in block.items) > 1800):
            findings.append(
                Finding(
                    code="editorial.long_list",
                    severity="warning",
                    block_id=block.id,
                    message="Длинный список: проверьте структуру, разделите требования и добавьте критерии приёмки.",
                )
            )
        if re.search(
            r"\b(?:TODO|FIXME|TBD)\b|\{\{.+?\}\}|\[ЗАПОЛНИТЬ\]", serialized_text, flags=re.IGNORECASE
        ):
            error("content.placeholder", "Найдена незаполненная заглушка.", block.id)
        restricted_names = [
            source.name
            for source in manifest.sources
            if source.role in {"context_only", "requirements_style"}
        ] + [source.name for source in manifest.excluded_sources]
        for name in restricted_names:
            if len(name) >= 5 and name.casefold() in serialized_text.casefold():
                error(
                    "source.mention",
                    f"Упоминание запрещённого для ссылок документа: {name}",
                    block.id,
                )
    return findings


def _block_texts(block):
    if block.type == "requirement":
        return [block.id, block.text, block.acceptance, block.subsystem, block.priority]
    if block.type in {"paragraph", "heading", "equation", "appendix"}:
        return [block.text]
    if block.type == "table":
        return [block.caption, *(text for row in block.rows for text in row)]
    if block.type == "image":
        return [block.caption]
    if block.type == "list":
        return block.items
    return []


def check_docx(path: Path, template: Template, content: DocumentContent) -> list[Finding]:
    findings = []
    document = Document(str(path))
    for section in document.sections:
        for name, expected in template.page.model_dump().items():
            attr = {"width_mm": "page_width", "height_mm": "page_height"}.get(
                name, name.replace("_mm", "_margin")
            )
            actual = getattr(section, attr)
            if actual is None or abs(actual.mm - expected) > 0.15:
                findings.append(
                    Finding(code="docx.page", message=f"Параметр страницы {name} отличается от шаблона.")
                )
    normal = document.styles["Normal"]
    assert isinstance(normal, ParagraphStyle)
    if normal.font.name != template.text.font:
        findings.append(Finding(code="docx.font", message="Основной шрифт DOCX отличается от шаблона."))
    if normal.font.size is None or abs(normal.font.size.pt - template.text.size_pt) > 0.1:
        findings.append(
            Finding(code="docx.font_size", message="Размер основного шрифта отличается от шаблона.")
        )
    if (
        normal.paragraph_format.first_line_indent is None
        or abs(normal.paragraph_format.first_line_indent.mm - template.text.indent_mm) > 0.15
    ):
        findings.append(Finding(code="docx.indent", message="Абзацный отступ отличается от шаблона."))
    spacing = normal.paragraph_format.line_spacing
    if spacing is None or abs(float(spacing) - template.text.line_spacing) > 0.02:
        findings.append(Finding(code="docx.spacing", message="Межстрочный интервал отличается от шаблона."))
    footer_xml = "".join(section.footer._element.xml for section in document.sections)
    if "PAGE" not in footer_xml:
        findings.append(Finding(code="docx.page_numbers", message="Не найдено поле номера страницы."))
    if template.title_page and any(
        "PAGE" in section.first_page_footer._element.xml for section in document.sections
    ):
        findings.append(
            Finding(
                code="docx.title_page_number",
                message="На титульной странице ошибочно присутствует номер страницы.",
            )
        )
    if any(
        document.styles[name].element.find(".//" + qn("w:pBdr")) is not None for name in ("Title", "Normal")
    ):
        findings.append(
            Finding(
                code="docx.decorative_border",
                message="В стиле титула или основного текста обнаружена непредусмотренная рамка/линия.",
            )
        )
    math_text = "".join(element.text or "" for element in document.element.iter(qn("m:t")))
    for block in content.blocks:
        if block.type == "equation" and _normalize(block.text) not in _normalize(math_text):
            findings.append(
                Finding(
                    code="docx.equation",
                    message="Формула потеряна при преобразовании DOCX.",
                    block_id=block.id,
                )
            )
    xml_text = " ".join(element.text or "" for element in document.element.iter(qn("w:t")))
    for block in content.blocks:
        if block.type == "paragraph" and _normalize(block.text) not in _normalize(xml_text):
            findings.append(
                Finding(code="docx.text", message="Текст блока не найден в итоговом DOCX.", block_id=block.id)
            )
    if template.toc:
        instructions = " ".join(element.text or "" for element in document.element.iter(qn("w:instrText")))
        if "TOC" not in instructions:
            findings.append(Finding(code="docx.toc", message="Не найдено поле оглавления."))
    return findings


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value.replace("\u00ad", "")).casefold()


def inspect_pdf(path: Path, content: DocumentContent, template: Template, pages_dir: Path) -> dict:
    findings = []
    candidates = set()
    page_hashes = {}
    block_pages = {}
    pages_dir.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(path) as pdf:
        if template.title_page and template.title_layout and len(pdf):
            first = pdf.load_page(0)
            layout = template.title_layout
            first_text = _normalize(first.get_textpage().extractText())
            for index, block in enumerate(layout.blocks):
                texts = (
                    title_text(block.text, content.metadata).splitlines()
                    if block.kind == "text"
                    else [person.name for person in people(content.metadata, block.kind)]
                )
                for text in texts:
                    if text.strip() and _normalize(text) not in first_text:
                        findings.append(
                            Finding(
                                code="title.missing",
                                message=f"Текст блока титула {index + 1} не найден на первой странице: {text}",
                                page=1,
                            )
                        )
                    boxes = first.search_for(text) if text.strip() else []
                    top = (template.page.top_mm + block.y_mm) * 72 / 25.4
                    bottom = top + block.height_mm * 72 / 25.4
                    if boxes and not any(box.y0 >= top - 5 and box.y1 <= bottom + 5 for box in boxes):
                        findings.append(
                            Finding(
                                code="title.position",
                                message=f"Блок титула {index + 1} смещён или переполнен. Увеличьте высоту блока или уменьшите текст/шрифт.",
                                page=1,
                            )
                        )
            bottom_text = title_text(layout.bottom_text, content.metadata)
            if bottom_text:
                boxes = first.search_for(bottom_text)
                expected = (template.page.height_mm - template.page.bottom_mm - 3) * 72 / 25.4
                if not boxes or abs(max(box.y1 for box in boxes) - expected) > 12:
                    findings.append(
                        Finding(
                            code="title.bottom",
                            message="Закреплённый нижний блок титула отсутствует или смещён относительно нижнего поля.",
                            page=1,
                        )
                    )
        full_text = "\n".join(pdf.load_page(i).get_textpage().extractText() for i in range(len(pdf)))
        all_fonts = set()
        for page_index in range(len(pdf)):
            number = page_index + 1
            page = pdf.load_page(page_index)
            page_text = page.get_textpage().extractText().strip()
            if (
                number > (2 if template.toc and template.title_page else 1)
                and len(page_text) < 250
                and not page.get_images()
            ):
                findings.append(
                    Finding(
                        code="editorial.sparse_page",
                        severity="warning",
                        page=number,
                        message="Мало текста на странице: проверьте разрывы и переносы. Для отдельных разделов это допустимо.",
                    )
                )
                candidates.add(number)
            if not page_text and not page.get_images():
                findings.append(Finding(code="pdf.blank", message="Пустая страница.", page=number))
            if (
                abs(page.rect.width * 25.4 / 72 - template.page.width_mm) > 0.2
                or abs(page.rect.height * 25.4 / 72 - template.page.height_mm) > 0.2
            ):
                findings.append(
                    Finding(
                        code="pdf.page_size",
                        message="Размер страницы PDF отличается от шаблона.",
                        page=number,
                    )
                )
            for font in page.get_fonts():
                all_fonts.add(font[3])
            for block in page.get_textpage().extractDICT()["blocks"]:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        color = span.get("color", 0)
                        components = [(color >> shift) & 255 for shift in (16, 8, 0)]
                        if min(components) > 235 and span.get("text", "").strip():
                            findings.append(
                                Finding(
                                    code="pdf.invisible_text",
                                    message="Очень светлый текст на белом листе; проверьте цвет формулы или текста.",
                                    page=number,
                                )
                            )
                            candidates.add(number)
                rect = pymupdf.Rect(block["bbox"])
                if (
                    rect.x0 < -0.5
                    or rect.y0 < -0.5
                    or rect.x1 > page.rect.width + 0.5
                    or rect.y1 > page.rect.height + 0.5
                ):
                    findings.append(
                        Finding(
                            code="pdf.overflow",
                            message="Содержимое выходит за границы страницы.",
                            page=number,
                        )
                    )
                    candidates.add(number)
                # Glyph boxes and trailing justification spaces can exceed the advance width by 5 pt.
                if (
                    rect.x0 < template.page.left_mm * 72 / 25.4 - 5
                    or rect.x1 > page.rect.width - template.page.right_mm * 72 / 25.4 + 5
                ):
                    findings.append(
                        Finding(
                            code="pdf.margin",
                            severity="warning",
                            message="Элемент выходит в боковое поле; требуется просмотр.",
                            page=number,
                        )
                    )
                    candidates.add(number)
            for block in content.blocks:
                texts = _block_texts(block)
                snippet = texts[0][:65] if texts else ""
                if snippet and page.search_for(snippet):
                    block_pages.setdefault(block.id, []).append(number)
                    if block.type in {"image", "table", "equation", "appendix"}:
                        candidates.add(number)
            output = pages_dir / f"page-{number}.png"
            # Deterministic pixels allow unchanged pages to retain their visual-review record.
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
            pixmap.save(output)
            page_hashes[str(number)] = digest(output)
        if len(pdf):
            candidates.update([1, len(pdf)])
            if template.toc:
                candidates.add(2 if template.title_page and len(pdf) > 1 else 1)
        if template.text.font == "Times New Roman" and not any(
            "timesnewroman" in name.replace(" ", "").casefold() for name in all_fonts
        ):
            findings.append(
                Finding(
                    code="pdf.font_substitution",
                    message="В PDF не найден Times New Roman; возможна подмена шрифта.",
                )
            )
        for block in content.blocks:
            if block.type == "equation" and _normalize(block.text) not in _normalize(full_text):
                findings.append(
                    Finding(
                        code="pdf.equation",
                        message="Текст формулы не найден в PDF. Нужна проверка математического рендеринга.",
                        block_id=block.id,
                    )
                )
            if block.type in {"paragraph", "heading", "list", "table", "image", "requirement"}:
                for text in _block_texts(block):
                    # Text can be hyphenated by the renderer. Compare words, excluding layout punctuation.
                    words = set(re.findall(r"[\w]{3,}", text.casefold()))
                    rendered = set(re.findall(r"[\w]{3,}", full_text.casefold()))
                    if words and len(words - rendered) / len(words) > 0.15:
                        findings.append(
                            Finding(
                                code="pdf.text_missing",
                                message="Часть текста блока не найдена в PDF; проверьте отображение.",
                                block_id=block.id,
                            )
                        )
                        break
        return {
            "findings": findings,
            "page_count": len(pdf),
            "review_pages": sorted(candidates),
            "page_hashes": page_hashes,
            "block_pages": block_pages,
            "fonts": sorted(all_fonts),
        }

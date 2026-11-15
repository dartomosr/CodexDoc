"""Deterministic, cached source inspection. No model calls or external uploads."""

import shutil
from collections import Counter
from pathlib import Path
from typing import Literal

import pymupdf
from docx import Document
from docx.styles.style import ParagraphStyle
from PIL import Image

from codexdoc.storage import DATA, digest, read_json, write_json

ANALYZER_VERSION = 1
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".rst",
    ".csv",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".html",
    ".css",
    ".sql",
    ".toml",
    ".typ",
    ".tex",
    ".log",
    ".ini",
}
MAX_TEXT = 2_000_000


def _length(value):
    return round(value.mm, 2) if value is not None else None


def _font(style):
    chain = []
    while style is not None and style.style_id not in chain:
        chain.append(style.style_id)
        if style.font.name:
            return style.font.name
        style = style.base_style
    return None


def analyze(path: Path, cache_dir: Path = DATA / "cache") -> dict:
    if not path.is_file():
        raise ValueError(f"Файл не найден: {path}")
    if path.stat().st_size > 250 * 1024 * 1024:
        raise ValueError(f"Файл больше 250 МБ: {path.name}. Разделите материал на части.")
    sha = digest(path)
    cached = cache_dir / f"v{ANALYZER_VERSION}-{sha}.json"
    if cached.exists():
        return read_json(cached)
    result = {
        "analyzer_version": ANALYZER_VERSION,
        "sha256": sha,
        "format": path.suffix.lower(),
        "text": "",
        "warnings": [],
        "measured": {},
    }
    ext = path.suffix.lower()
    if ext == ".docx":
        document = Document(str(path))
        result["text"] = "\n".join(p.text for p in document.paragraphs)
        result["text"] += "\n" + "\n".join(
            " | ".join(c.text for c in row.cells) for t in document.tables for row in t.rows
        )
        sections = [
            {
                "width_mm": _length(s.page_width),
                "height_mm": _length(s.page_height),
                "left_mm": _length(s.left_margin),
                "right_mm": _length(s.right_margin),
                "top_mm": _length(s.top_margin),
                "bottom_mm": _length(s.bottom_margin),
            }
            for s in document.sections
        ]
        normal = document.styles["Normal"]
        assert isinstance(normal, ParagraphStyle)
        pf = normal.paragraph_format
        text_style = {
            "font": _font(normal),
            "size_pt": normal.font.size.pt if normal.font.size else None,
            "line_spacing": pf.line_spacing if isinstance(pf.line_spacing, float) else None,
            "indent_mm": _length(pf.first_line_indent),
        }
        run_fonts = Counter(
            r.font.name for p in document.paragraphs for r in p.runs if r.text and r.font.name
        )
        run_sizes = Counter(
            r.font.size.pt for p in document.paragraphs for r in p.runs if r.text and r.font.size
        )
        result["measured"] = {
            "sections": sections,
            "normal_style": text_style,
            "direct_fonts": dict(run_fonts),
            "direct_sizes": {str(k): v for k, v in run_sizes.items()},
            "headings": [
                {"text": p.text, "style": p.style.name}
                for p in document.paragraphs
                if p.style is not None and (p.style.name or "").startswith("Heading")
            ],
            "table_count": len(document.tables),
            "image_count": len(document.inline_shapes),
            "headers": [s.header.paragraphs[0].text for s in document.sections],
            "footers": [s.footer.paragraphs[0].text for s in document.sections],
        }
        result["warnings"].append(
            "Измерены секции, стили и прямое форматирование. Наследование стилей, титул, исключения и смысл требований проверяет агент."
        )
    elif ext == ".pdf":
        with pymupdf.open(path) as document:
            if document.needs_pass:
                raise ValueError("PDF защищён паролем. Предоставьте доступную копию.")
            fonts = Counter()
            sizes = Counter()
            pages = []
            text = []
            for page in document:
                text.append(page.get_text())
                pages.append(
                    {
                        "width_mm": round(page.rect.width * 25.4 / 72, 2),
                        "height_mm": round(page.rect.height * 25.4 / 72, 2),
                    }
                )
                for block in page.get_textpage().extractDICT()["blocks"]:
                    for line in block.get("lines", []):
                        for span in line["spans"]:
                            fonts[span["font"]] += len(span["text"])
                            sizes[round(span["size"], 1)] += len(span["text"])
            result["text"] = "\n\n".join(text)
            result["measured"] = {
                "pages": pages,
                "fonts": dict(fonts),
                "sizes": {str(k): v for k, v in sizes.items()},
            }
            if not result["text"].strip():
                result["warnings"].append(
                    "Скан без текстового слоя. Нужен визуальный анализ агентом; OCR автоматически не выполнялся."
                )
            result["warnings"].append(
                "PDF не сохраняет исходные стили и поля DOCX. Размер страницы измерен, поля и структуру должен подтвердить агент."
            )
    elif ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
        with Image.open(path) as picture:
            result["measured"] = {
                "width_px": picture.width,
                "height_px": picture.height,
                "mode": picture.mode,
            }
        result["warnings"].append(
            "Изображение сохранено для визуального анализа; содержание не распознано автоматически."
        )
    elif ext in TEXT_EXTENSIONS:
        raw = path.read_bytes()
        try:
            result["text"] = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            result["text"] = raw.decode("cp1251", errors="replace")
            result["warnings"].append("Использована кодировка Windows-1251; проверьте читаемость текста.")
    else:
        result["warnings"].append(
            "Формат сохранён как вложение; автоматическое извлечение не поддерживается."
        )
    if len(result["text"]) > MAX_TEXT:
        result["text"] = result["text"][:MAX_TEXT]
        result["warnings"].append("Извлечённый текст ограничен 2 млн символов; полный оригинал сохранён.")
    write_json(cached, result)
    return result


def export_analysis(path: Path, output: Path, cache_dir: Path = DATA / "cache") -> dict:
    result = analyze(path, cache_dir)
    write_json(output, result)
    return result


def copy_source(path: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)


def guess_role(path: Path) -> Literal["material", "standard", "code", "image"]:
    if any(part in path.stem.lower() for part in ["гост", "методич", "требован", "standard"]):
        return "standard"
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        return "image"
    if path.suffix.lower() in {".py", ".js", ".ts", ".cpp", ".c", ".cs", ".sql", ".html"}:
        return "code"
    return "material"

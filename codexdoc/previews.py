"""Local, bounded previews of source PDF pages."""

from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageOps

from codexdoc.models import Workspace
from codexdoc.storage import contained, digest, read_json


def page_overview(workspace: Path) -> Path:
    """A bounded contact sheet per 24 pages, with explicit page numbers."""
    pages = sorted(
        (workspace / "output" / "pages").glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1])
    )
    generated = set()
    for offset in range(0, len(pages), 24):
        batch = pages[offset : offset + 24]
        sheet = Image.new("RGB", (4 * 210, ((len(batch) + 3) // 4) * 315), "#dddddd")
        draw = ImageDraw.Draw(sheet)
        for i, path in enumerate(batch):
            with Image.open(path) as original:
                thumb = ImageOps.contain(original.convert("RGB"), (190, 280))
                x, y = (i % 4) * 210 + 10, (i // 4) * 315 + 22
                sheet.paste(thumb, (x, y))
                draw.text((x, y - 18), path.stem.split("-")[-1], fill="black")
        target = (
            workspace / "output" / ("overview.png" if offset == 0 else f"overview-{offset // 24 + 1}.png")
        )
        sheet.save(target)
        generated.add(target)
    for previous in (workspace / "output").glob("overview*.png"):
        if previous not in generated and (
            previous.name == "overview.png" or previous.stem.removeprefix("overview-").isdigit()
        ):
            previous.unlink()
    return workspace / "output" / "overview.png"


def render_source_pages(workspace: Path, source_id: str, pages: list[int] | None = None) -> list[Path]:
    workspace = workspace.resolve()
    manifest = Workspace.model_validate(read_json(workspace / "workspace.json"))
    source = next((item for item in manifest.sources if item.id == source_id), None)
    if source is None:
        if any(item.id == source_id for item in manifest.excluded_sources):
            raise ValueError(f"Предпросмотр запрещён для исключённого файла: {source_id}")
        raise ValueError(f"Неизвестный id источника: {source_id}")
    if not source.path:
        raise ValueError(f"Источник {source_id} запрещён для просмотра или отсутствует в рабочей папке.")
    source_path = contained(workspace, source.path)
    if source_path.suffix.casefold() != ".pdf":
        raise ValueError(f"Источник {source_id} не является PDF.")
    if source.sha256 and digest(source_path) != source.sha256:
        raise ValueError(f"PDF источника {source_id} изменился; сначала обновите его анализ.")
    requested = list(dict.fromkeys(pages or [1]))
    if not requested or any(page < 1 for page in requested):
        raise ValueError("Номера страниц должны быть положительными.")
    if len(requested) > 20:
        raise ValueError("За один вызов можно отрендерить не более 20 страниц.")

    destination = contained(workspace, f"analysis/previews/{source_id}")
    destination.mkdir(parents=True, exist_ok=True)
    results = []
    with pymupdf.open(source_path) as document:
        if document.needs_pass:
            raise ValueError(f"PDF источника {source_id} защищён паролем.")
        for page_number in requested:
            if page_number > document.page_count:
                raise ValueError(
                    f"В источнике {source_id} {document.page_count} стр.; запрошена {page_number}."
                )
            path = destination / f"page-{page_number}.png"
            document.load_page(page_number - 1).get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).save(
                path
            )
            results.append(path)
    return results

"""Runs exclusively under the bundled LibreOffice Python (3.10)."""

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import uno
from com.sun.star.beans import PropertyValue


def prop(name, value):
    item = PropertyValue()
    item.Name = name
    item.Value = value
    return item


def configure_contents(document, index, options):
    from com.sun.star.style import LineSpacing

    settings = options.get("contents_format")
    if not settings:
        return
    index.Level = settings["depth"]
    styles = document.getStyleFamilies().getByName("ParagraphStyles")
    spec = settings["entries"]
    for level in range(1, settings["depth"] + 1):
        name = "CodexDocContents" + str(level)
        if not styles.hasByName(name):
            styles.insertByName(name, document.createInstance("com.sun.star.style.ParagraphStyle"))
        style = styles.getByName(name)
        style.CharFontName = spec["font"] or options["font"]
        style.CharHeight = spec["size_pt"]
        style.CharColor = 0
        style.CharWeight = 150.0 if spec["bold"] else 100.0
        style.ParaLeftMargin = round((spec["left_mm"] + (level - 1) * settings["level_indent_mm"]) * 100)
        style.ParaRightMargin = round(spec["right_mm"] * 100)
        style.ParaFirstLineIndent = round(spec["first_line_mm"] * 100)
        style.ParaTopMargin = round(spec["before_pt"] * 25.4 / 72 * 100)
        style.ParaBottomMargin = round(spec["after_pt"] * 25.4 / 72 * 100)
        style.ParaAdjust = {"left": 0, "right": 1, "justify": 2, "center": 3}[spec["alignment"]]
        spacing = LineSpacing()
        spacing.Mode = 0  # proportional
        spacing.Height = round(spec["line_spacing"] * 100)
        style.ParaLineSpacing = spacing
        index.setPropertyValue("ParaStyleLevel" + str(level), name)


def main():
    source, docx, pdf, office = sys.argv[1:]
    options_path = Path(source).with_suffix(".render.json")
    options = json.loads(options_path.read_text(encoding="utf-8")) if options_path.exists() else {}
    pipe = "codexdoc_" + uuid.uuid4().hex
    desktop = document = None
    with tempfile.TemporaryDirectory(prefix="codexdoc-lo-") as profile:
        arguments = [
            office,
            "-env:UserInstallation=" + Path(profile).as_uri(),
            "--headless",
            "--norestore",
            "--nodefault",
            "--nofirststartwizard",
            "--accept=pipe,name=" + pipe + ";urp;StarOffice.ServiceManager",
        ]
        server = subprocess.Popen(
            arguments,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            local = uno.getComponentContext()
            resolver = local.ServiceManager.createInstanceWithContext(
                "com.sun.star.bridge.UnoUrlResolver", local
            )
            context = None
            for _ in range(150):
                try:
                    context = resolver.resolve("uno:pipe,name=" + pipe + ";urp;StarOffice.ComponentContext")
                    break
                except Exception:
                    if server.poll() is not None:
                        raise RuntimeError("Office process exited before accepting connection") from None
                    time.sleep(0.2)
            if context is None:
                raise RuntimeError("Private renderer did not start within 30 seconds")
            desktop = context.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", context)
            document = desktop.loadComponentFromURL(
                Path(source).as_uri(),
                "_blank",
                0,
                (
                    prop("Hidden", True),
                    prop("ReadOnly", False),
                    prop("UpdateDocMode", 0),
                    prop("MacroExecutionMode", 4),
                ),
            )
            if document is None:
                raise RuntimeError("DOCX could not be loaded")
            embedded_objects = document.getEmbeddedObjects()
            for name in embedded_objects.getElementNames():
                embedded = embedded_objects.getByName(name).getEmbeddedObject()
                if hasattr(embedded, "Formula"):
                    # Initialize the formula's rendering model before PDF export in a fresh profile.
                    formula = embedded.Formula
                    # Automatic math colour can inherit white from Windows dark mode,
                    # even though Writer exports a white page. Make print colour explicit.
                    embedded.Formula = "color black { " + formula + " }"
                    print(json.dumps({"formula_initialized": name, "formula": formula}))
            indexes = document.getDocumentIndexes()
            if indexes.getCount() == 0 and document.getBookmarks().hasByName("CodexDocTOC"):
                anchor = document.getBookmarks().getByName("CodexDocTOC").getAnchor()
                index = document.createInstance("com.sun.star.text.ContentIndex")
                index.CreateFromOutline = True
                index.Level = 3
                index.Title = ""
                anchor.getText().insertTextContent(anchor, index, False)
                indexes = document.getDocumentIndexes()
            for i in range(indexes.getCount()):
                configure_contents(document, indexes.getByIndex(i), options)
            for _ in range(2):
                document.refresh()
                for i in range(indexes.getCount()):
                    indexes.getByIndex(i).update()
            # Saving can invalidate UNO index proxies. Capture diagnostics while
            # the document is still in its original editing session.
            indexes_updated = indexes.getCount()
            document.storeAsURL(
                Path(docx).as_uri(), (prop("FilterName", "Office Open XML Text"), prop("Overwrite", True))
            )
            document.storeToURL(
                Path(pdf).as_uri(),
                (
                    prop("FilterName", "writer_pdf_Export"),
                    prop("Overwrite", True),
                    prop(
                        "FilterData",
                        (
                            prop("ExportBookmarks", True),
                            prop("ExportDocumentProperties", False),
                            prop("UseTaggedPDF", True),
                        ),
                    ),
                ),
            )
            print(json.dumps({"indexes_updated": indexes_updated, "docx": docx, "pdf": pdf}))
        finally:
            if document is not None:
                try:
                    document.close(True)
                except Exception as exc:
                    # A disposed bridge cannot be closed twice. Cleanup must
                    # neither mask an export exception nor reject saved files;
                    # the caller verifies both artifacts and their contents.
                    print("Document cleanup: " + str(exc), file=sys.stderr)
            if desktop is not None:
                try:
                    desktop.terminate()
                except Exception as exc:
                    print("Office cleanup: " + str(exc), file=sys.stderr)
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                subprocess.run(
                    ["taskkill", "/PID", str(server.pid), "/T", "/F"], capture_output=True, check=False
                )
                server.wait(timeout=5)


if __name__ == "__main__":
    main()

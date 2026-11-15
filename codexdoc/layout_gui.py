"""Editors for declarative print layout. No raw configuration editing required."""

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from codexdoc.models import BibliographyFormat, ContentsFormat, ParagraphStyleSpec, Template, TitleLayout


def choices(items, value):
    field = QComboBox()
    for title, key in items:
        field.addItem(title, key)
    field.setCurrentIndex(max(0, field.findData(value)))
    return field


def number(value, low, high):
    field = QDoubleSpinBox()
    field.setRange(low, high)
    field.setValue(value)
    return field


class StyleEditor(QWidget):
    def __init__(self, spec: ParagraphStyleSpec):
        super().__init__()
        form = QFormLayout(self)
        self.font_input = QLineEdit(spec.font)
        self.font_input.setPlaceholderText("Как в основном тексте")
        form.addRow("Шрифт", self.font_input)
        self.fields = {}
        for key, label, low, high in [
            ("size_pt", "Кегль, пт", 8, 36),
            ("line_spacing", "Интервал", 1, 3),
            ("first_line_mm", "Первая строка, мм (минус: выступ)", -30, 50),
            ("left_mm", "Отступ слева, мм", 0, 70),
            ("right_mm", "Отступ справа, мм", 0, 70),
            ("before_pt", "До абзаца, пт", 0, 100),
            ("after_pt", "После абзаца, пт", 0, 100),
        ]:
            field = number(getattr(spec, key), low, high)
            self.fields[key] = field
            form.addRow(label, field)
        self.align = choices(
            [("Слева", "left"), ("По центру", "center"), ("Справа", "right"), ("По ширине", "justify")],
            spec.alignment,
        )
        self.bold = QCheckBox("Полужирный")
        self.bold.setChecked(spec.bold)
        form.addRow("Выравнивание", self.align)
        form.addRow(self.bold)

    def value(self):
        return ParagraphStyleSpec.model_validate(
            {
                "font": self.font_input.text().strip(),
                "alignment": self.align.currentData(),
                "bold": self.bold.isChecked(),
                **{key: field.value() for key, field in self.fields.items()},
            }
        )


class LayoutDialog(QDialog):
    def __init__(self, template: Template, parent=None):
        super().__init__(parent)
        self.original = template
        self.setWindowTitle("Титул, оглавление и библиография")
        self.resize(940, 740)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.blocks = []
        self.make_title(template)
        self.make_sections(template)
        controls = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        controls.accepted.connect(self.try_accept)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)

    def make_title(self, template):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.title_enabled = QCheckBox("Использовать конструктор титула (выключено: прежний макет)")
        self.title_enabled.setChecked(template.title_layout is not None)
        layout.addWidget(self.title_enabled)
        note = QLabel(
            "Y отсчитывается от верхнего поля страницы. Блоки идут сверху вниз и не пересекаются. Подстановки: {organization}, {department}, {kind}, {title}, {author}, {group}, {supervisor}, {city}, {year}. Строки авторов берутся из титульных данных задания."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Тип", "Текст", "Y, мм", "Высота, мм"])
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(1, 420)
        layout.addWidget(self.table, 1)
        self.title_data = template.title_layout or TitleLayout()
        for block in self.title_data.blocks:
            self.add_block(block.model_dump())
        buttons = QHBoxLayout()
        for text, callback in [
            ("Добавить блок", lambda: self.add_block()),
            ("Убрать блок", self.remove_block),
            ("Шрифт и интервалы блока…", self.edit_block_style),
        ]:
            b = QPushButton(text)
            b.clicked.connect(callback)
            buttons.addWidget(b)
        layout.addLayout(buttons)
        form = QFormLayout()
        self.bottom = QLineEdit(self.title_data.bottom_text)
        self.bottom_height = number(self.title_data.bottom_height_mm, 4, 60)
        form.addRow("Закреплённый нижний блок", self.bottom)
        form.addRow("Высота нижнего блока, мм", self.bottom_height)
        layout.addLayout(form)
        self.bottom_style = self.title_data.bottom_style
        b = QPushButton("Шрифт нижнего блока…")
        b.clicked.connect(self.edit_bottom_style)
        layout.addWidget(b)
        self.tabs.addTab(page, "Титул")

    def add_block(self, data=None):
        data = data or {
            "kind": "text",
            "text": "",
            "y_mm": 0,
            "height_mm": 15,
            "style": ParagraphStyleSpec().model_dump(),
        }
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setCellWidget(
            row,
            0,
            choices(
                [("Текст", "text"), ("Авторы и подписи", "authors"), ("Проверяющие", "reviewers")],
                data["kind"],
            ),
        )
        self.table.setItem(row, 1, QTableWidgetItem(data["text"]))
        self.table.setCellWidget(row, 2, number(data["y_mm"], 0, 450))
        self.table.setCellWidget(row, 3, number(data["height_mm"], 4, 250))
        self.table.setRowHeight(row, 44)
        self.blocks.append(ParagraphStyleSpec.model_validate(data["style"]))

    def remove_block(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
            self.blocks.pop(row)

    def style_dialog(self, style):
        dialog = QDialog(self)
        dialog.setWindowTitle("Оформление блока")
        layout = QVBoxLayout(dialog)
        editor = StyleEditor(style)
        layout.addWidget(editor)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return editor.value() if dialog.exec() == QDialog.DialogCode.Accepted else style

    def edit_block_style(self):
        row = self.table.currentRow()
        if row >= 0:
            self.blocks[row] = self.style_dialog(self.blocks[row])

    def edit_bottom_style(self):
        self.bottom_style = self.style_dialog(self.bottom_style)

    def make_sections(self, template):
        self.section_controls = {}
        for key, label, spec in [
            ("contents_format", "Оглавление", template.contents_format or ContentsFormat()),
            ("bibliography_format", "Библиография", template.bibliography_format or BibliographyFormat()),
        ]:
            page = QWidget()
            form = QFormLayout(page)
            enabled = QCheckBox("Использовать отдельные настройки")
            enabled.setChecked(getattr(template, key) is not None)
            title = QLineEdit(spec.title)
            heading = StyleEditor(spec.heading)
            entries = StyleEditor(spec.entries)
            form.addRow(enabled)
            form.addRow("Название раздела", title)
            form.addRow(QLabel("Заголовок"))
            form.addRow(heading)
            form.addRow(QLabel("Записи"))
            form.addRow(entries)
            controls = {"enabled": enabled, "title": title, "heading": heading, "entries": entries}
            if key == "contents_format":
                controls["depth"] = number(spec.depth, 1, 3)
                controls["level_indent_mm"] = number(spec.level_indent_mm, 0, 20)
                form.addRow("Уровней заголовков", controls["depth"])
                form.addRow("Отступ следующего уровня, мм", controls["level_indent_mm"])
            else:
                controls["numbering"] = choices(
                    [("1. Источник", "decimal"), ("[1] Источник", "brackets"), ("Без номеров", "none")],
                    spec.numbering,
                )
                form.addRow("Нумерация записей", controls["numbering"])
                for attr, text in [
                    ("heading_numbered", "Нумеровать заголовок раздела"),
                    ("include_in_toc", "Включать в оглавление"),
                    ("new_page", "Начинать с новой страницы"),
                ]:
                    controls[attr] = QCheckBox(text)
                    controls[attr].setChecked(getattr(spec, attr))
                    form.addRow(controls[attr])
            self.section_controls[key] = controls
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.tabs.addTab(scroll, label)
        page = QWidget()
        layout = QVBoxLayout(page)
        self.structural_enabled = QCheckBox("Отдельный стиль ненумерованных структурных заголовков")
        self.structural_enabled.setChecked(template.structural_heading_style is not None)
        self.structural_numbered = QCheckBox(
            "Включать структурные заголовки первого уровня в общую нумерацию"
        )
        self.structural_numbered.setChecked(template.structural_numbered)
        self.structural_style = StyleEditor(
            template.structural_heading_style or ParagraphStyleSpec(alignment="center", bold=True)
        )
        layout.addWidget(self.structural_enabled)
        layout.addWidget(self.structural_numbered)
        layout.addWidget(self.structural_style)
        layout.addStretch()
        self.tabs.addTab(page, "Структурные заголовки")

    def values(self):
        result = {
            "title_layout": None,
            "structural_numbered": self.structural_numbered.isChecked(),
            "structural_heading_style": self.structural_style.value().model_dump()
            if self.structural_enabled.isChecked()
            else None,
        }
        if self.title_enabled.isChecked():
            blocks = []
            for row, style in enumerate(self.blocks):
                kind = self.table.cellWidget(row, 0)
                y = self.table.cellWidget(row, 2)
                height = self.table.cellWidget(row, 3)
                item = self.table.item(row, 1)
                assert (
                    isinstance(kind, QComboBox)
                    and isinstance(y, QDoubleSpinBox)
                    and isinstance(height, QDoubleSpinBox)
                    and item is not None
                )
                blocks.append(
                    {
                        "kind": kind.currentData(),
                        "text": item.text(),
                        "y_mm": y.value(),
                        "height_mm": height.value(),
                        "style": style.model_dump(),
                    }
                )
            result["title_layout"] = TitleLayout.model_validate(
                {
                    "blocks": blocks,
                    "bottom_text": self.bottom.text(),
                    "bottom_height_mm": self.bottom_height.value(),
                    "bottom_style": self.bottom_style.model_dump(),
                }
            ).model_dump()
        for key, controls in self.section_controls.items():
            if not controls["enabled"].isChecked():
                result[key] = None
                continue
            data = {
                "title": controls["title"].text(),
                "heading": controls["heading"].value().model_dump(),
                "entries": controls["entries"].value().model_dump(),
            }
            if key == "contents_format":
                data.update(
                    depth=int(controls["depth"].value()), level_indent_mm=controls["level_indent_mm"].value()
                )
                result[key] = ContentsFormat.model_validate(data).model_dump()
            else:
                data["numbering"] = controls["numbering"].currentData()
                data.update(
                    {
                        attr: controls[attr].isChecked()
                        for attr in ("heading_numbered", "include_in_toc", "new_page")
                    }
                )
                result[key] = BibliographyFormat.model_validate(data).model_dump()
        return result

    def try_accept(self):
        try:
            Template.model_validate({**self.original.model_dump(), **self.values()})
            self.accept()
        except ValueError as exc:
            QMessageBox.warning(self, "Проверьте макет", str(exc))

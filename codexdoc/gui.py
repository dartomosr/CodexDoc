"""Compact native Windows UI; expensive operations run outside the UI thread."""

import importlib
import logging
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QApplication,
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
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from codexdoc.layout_gui import LayoutDialog
from codexdoc.models import AgentProfile, CheckReport, Template
from codexdoc.pipeline import review
from codexdoc.storage import DATA, read_json
from codexdoc.studio_theme import studio_style

STYLE = studio_style()


def combo(items, value=None):
    widget = QComboBox()
    for title, data in items:
        widget.addItem(title, data)
    if value is not None:
        widget.setCurrentIndex(max(0, widget.findData(value)))
    return widget


def button(text, callback, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(callback)
    return widget


def muted(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setObjectName("muted")
    return label


def open_path(path: Path):
    if not path.exists():
        raise ValueError("Файл ещё не создан.")
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))


class WorkerSignals(QObject):
    success = Signal(object)
    failure = Signal(str)


class Worker(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            self.signals.success.emit(self.function())
        except Exception as exc:
            logging.exception("Background operation failed")
            self.signals.failure.emit(str(exc))


class ProfileDialog(QDialog):
    def __init__(self, profile: AgentProfile, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Профиль агента")
        self.setMinimumWidth(470)
        self.original = profile
        form = QFormLayout(self)
        self.name = QLineEdit(profile.name)
        self.autonomy = combo(
            [
                ("Согласовывать важные шаги", "guided"),
                ("Самостоятельно в рамках задания", "balanced"),
                ("Максимальная самостоятельность", "autonomous"),
            ],
            profile.autonomy,
        )
        self.verification = combo(
            [
                ("Быстрая: титул и проблемные страницы", "quick"),
                ("Адаптивная: ключевые и сложные страницы", "adaptive"),
                ("Тщательная: все страницы", "strict"),
            ],
            profile.verification,
        )
        self.internet = combo(
            [
                ("Не использовать", "off"),
                ("Сначала спросить", "ask"),
                ("Разрешён при необходимости", "allowed"),
            ],
            profile.internet,
        )
        self.errors = combo(
            [("Исправлять адресно", "repair"), ("Остановиться и сообщить", "stop")], profile.errors
        )
        self.privacy = QCheckBox("Защищать конфиденциальные материалы")
        self.privacy.setChecked(profile.confidential)
        for label, field in [
            ("Название", self.name),
            ("Самостоятельность", self.autonomy),
            ("Проверки", self.verification),
            ("Интернет", self.internet),
            ("При ошибках", self.errors),
        ]:
            form.addRow(label, field)
        form.addRow(self.privacy)
        self.save_copy = QCheckBox("Сохранить как отдельный профиль")
        self.save_copy.setChecked(profile.id in {"balanced", "careful", "autonomous"})
        form.addRow(self.save_copy)
        form.addRow(
            muted(
                "Эти настройки формируют инструкции агенту. Приложение не управляет сетевыми разрешениями самого агента и не отправляет материалы в облако."
            )
        )
        controls = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        controls.accepted.connect(self.accept)
        controls.rejected.connect(self.reject)
        form.addRow(controls)

    def value(self):
        return AgentProfile.model_validate(
            {
                "id": "profile-" + uuid.uuid4().hex[:10] if self.save_copy.isChecked() else self.original.id,
                "name": self.name.text().strip() or "Мой профиль",
                "autonomy": self.autonomy.currentData(),
                "verification": self.verification.currentData(),
                "internet": self.internet.currentData(),
                "errors": self.errors.currentData(),
                "confidential": self.privacy.isChecked(),
            }
        )


class TemplateDialog(QDialog):
    def __init__(self, template: Template, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройка шаблона")
        self.resize(560, 760)
        self.original = template
        self.layout_values = {}
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        panel = QWidget()
        self.form = QFormLayout(panel)
        self.name = QLineEdit(template.name)
        self.form.addRow("Название", self.name)
        self.fields = {}
        for name, title in [
            ("width_mm", "Ширина страницы, мм"),
            ("height_mm", "Высота страницы, мм"),
            ("left_mm", "Левое поле, мм"),
            ("right_mm", "Правое поле, мм"),
            ("top_mm", "Верхнее поле, мм"),
            ("bottom_mm", "Нижнее поле, мм"),
        ]:
            field = QDoubleSpinBox()
            field.setRange(5, 500)
            field.setValue(getattr(template.page, name))
            self.form.addRow(title, field)
            self.fields[name] = field
        self.font_input = QLineEdit(template.text.font)
        self.form.addRow("Шрифт", self.font_input)
        self.text_fields = {}
        for name, label, low, high in [
            ("size_pt", "Размер, пт", 8, 24),
            ("line_spacing", "Межстрочный интервал", 1, 3),
            ("indent_mm", "Абзацный отступ, мм", 0, 30),
            ("paragraph_after_pt", "После абзаца, пт", 0, 30),
        ]:
            field = QDoubleSpinBox()
            field.setRange(low, high)
            field.setValue(getattr(template.text, name))
            self.text_fields[name] = field
            self.form.addRow(label, field)
        self.flags = {}
        for name, label in [
            ("title_page", "Титульный лист"),
            ("toc", "Оглавление"),
            ("numbered_headings", "Нумерация разделов"),
            ("chapter_new_page", "Разделы с новой страницы"),
            ("heading_bold", "Полужирные заголовки"),
        ]:
            field = QCheckBox(label)
            field.setChecked(getattr(template, name))
            self.flags[name] = field
            self.form.addRow(field)
        self.header = QLineEdit(template.header)
        self.footer = QLineEdit(template.footer)
        self.required = QLineEdit("; ".join(template.required_sections))
        self.form.addRow("Верхний колонтитул", self.header)
        self.form.addRow("Нижний колонтитул", self.footer)
        self.form.addRow("Обязательные разделы через ;", self.required)
        self.form.addRow(button("Титул, оглавление и библиография…", self.edit_layout))
        self.notes = QPlainTextEdit(template.agent_notes)
        self.notes.setMaximumHeight(90)
        self.form.addRow("Особые требования", self.notes)
        self.form.addRow(
            muted(
                "Изменённое оформление сохраняется как отдельный пользовательский шаблон. Соответствие исходному ГОСТ после изменения параметров нужно проверить."
            )
        )
        scroll.setWidget(panel)
        layout.addWidget(scroll)
        controls = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        controls.accepted.connect(self.try_accept)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)

    def value(self):
        data = self.original.model_dump()
        data.update(
            {
                "id": "custom-" + uuid.uuid4().hex[:10],
                "name": self.name.text().strip(),
                "page": {key: field.value() for key, field in self.fields.items()},
                "text": {
                    "font": self.font_input.text().strip(),
                    **{key: field.value() for key, field in self.text_fields.items()},
                },
                **{key: field.isChecked() for key, field in self.flags.items()},
                "header": self.header.text(),
                "footer": self.footer.text(),
                "required_sections": [s.strip() for s in self.required.text().split(";") if s.strip()],
                "agent_notes": self.notes.toPlainText(),
                "revision": 1,
                **self.layout_values,
            }
        )
        return Template.model_validate(data)

    def edit_layout(self):
        try:
            dialog = LayoutDialog(self.value(), self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.layout_values = dialog.values()
        except ValueError as exc:
            QMessageBox.warning(self, "Проверьте настройки", str(exc))

    def try_accept(self):
        try:
            self.value()
            self.accept()
        except ValueError as exc:
            QMessageBox.warning(self, "Проверьте настройки", str(exc))


class ReviewDialog(QDialog):
    def __init__(self, workspace: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Просмотр результата")
        self.resize(970, 900)
        self.workspace = workspace
        self.report = CheckReport.model_validate(read_json(workspace / "output" / "checks.json"))
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.page = QSpinBox()
        self.page.setRange(1, max(1, self.report.page_count))
        self.zoom = combo([("По ширине", 0), ("100%", 100), ("150%", 150)], 0)
        controls.addWidget(button("Назад", lambda: self.page.setValue(self.page.value() - 1)))
        controls.addWidget(self.page)
        controls.addWidget(QLabel(f"из {self.report.page_count}"))
        controls.addWidget(button("Далее", lambda: self.page.setValue(self.page.value() + 1)))
        controls.addStretch()
        controls.addWidget(self.zoom)
        layout.addLayout(controls)
        self.info = muted("")
        layout.addWidget(self.info)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(False)
        self.picture = QLabel()
        self.scroll_area.setWidget(self.picture)
        layout.addWidget(self.scroll_area, 1)
        bottom = QHBoxLayout()
        self.note = QLineEdit()
        self.note.setPlaceholderText("Что проверено: поля, подписи, таблица, отсутствие обрезания…")
        bottom.addWidget(self.note, 1)
        self.mark = button("Отметить просмотр", self.mark_review, True)
        bottom.addWidget(self.mark)
        layout.addLayout(bottom)
        self.page.valueChanged.connect(self.show_page)
        self.zoom.currentIndexChanged.connect(self.show_page)
        self.show_page()

    def show_page(self):
        number = self.page.value()
        pixmap = QPixmap(str(self.workspace / "output" / "pages" / f"page-{number}.png"))
        factor = self.zoom.currentData()
        width = (
            max(400, self.scroll_area.viewport().width() - 24)
            if factor == 0
            else int(pixmap.width() * factor / 100)
        )
        pixmap = pixmap.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
        self.picture.setPixmap(pixmap)
        self.picture.resize(pixmap.size())
        can_mark = self.report.status not in {"blocked", "preview"}
        self.mark.setEnabled(can_mark)
        self.note.setEnabled(can_mark)
        text = (
            "Просмотр подтверждён"
            if number in self.report.reviewed_pages
            else "Требуется просмотр"
            if number in self.report.review_pages
            else "Дополнительная страница"
        )
        suffix = (
            " Это предпросмотр черновика: его нельзя отмечать как окончательную проверку."
            if self.report.status == "preview"
            else " Отмечайте только фактически просмотренное изображение текущей версии."
        )
        self.info.setText(text + "." + suffix)

    def mark_review(self):
        try:
            self.report = review(self.workspace, self.page.value(), self.note.text())
            self.show_page()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Просмотр не сохранён", str(exc))


def run() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=DATA / "application.log", encoding="utf-8", level=logging.INFO)
    application = QApplication.instance() or QApplication(sys.argv)
    assert isinstance(application, QApplication)
    application.setStyleSheet(STYLE)
    window = importlib.import_module("codexdoc.studio").MainWindow()
    window.show()
    return application.exec()


def __getattr__(name):
    if name == "MainWindow":
        return importlib.import_module("codexdoc.studio").MainWindow
    raise AttributeError(name)

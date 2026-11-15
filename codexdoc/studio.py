"""Stateful three-pane desktop workspace for CodexDoc."""

from __future__ import annotations

import json
import re
import shutil
import time
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QMimeData, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDrag,
    QFont,
    QKeySequence,
    QShortcut,
    QSyntaxHighlighter,
    QTextCharFormat,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from codexdoc import __version__
from codexdoc.analysis import TEXT_EXTENSIONS, analyze
from codexdoc.gui import ProfileDialog, ReviewDialog, TemplateDialog, Worker, button, muted, open_path
from codexdoc.instructions import generate_agents
from codexdoc.library import Library
from codexdoc.models import (
    AgentProfile,
    AuthorProfile,
    CheckReport,
    DraftExcludedSource,
    DraftSource,
    ExcludedSource,
    Metadata,
    Signatory,
    Source,
    SourceRole,
    Template,
    Workspace,
    WorkspaceDraft,
)
from codexdoc.pipeline import build, check, publish_template
from codexdoc.storage import ROOT, WORKSPACES, now, read_json, safe_name, write_json
from codexdoc.studio_state import current_build, title_fields
from codexdoc.studio_theme import status_tone, studio_style, tokens_for
from codexdoc.theme_controller import ThemeController
from codexdoc.workspaces import create_workspace, load_workspace, update_workspace

SOURCE_ROLE_LABELS: dict[SourceRole, str] = {
    "citable": "Можно цитировать",
    "context_only": "Только контекст",
    "requirements_style": "Требования и образец оформления",
}
SOURCE_ROLE_DIRECTORIES: dict[SourceRole, str] = {
    "citable": "sources",
    "context_only": "context",
    "requirements_style": "template",
}
SOURCE_MIME_TYPE = "application/x-codexdoc-source-id"
TREE_VIRTUAL_ROLE = Qt.ItemDataRole.UserRole + 1

STUDIO_STYLE = studio_style()


def _file_icon(path: Path) -> str:
    return {
        ".pdf": "PDF",
        ".docx": "DOC",
        ".doc": "DOC",
        ".xlsx": "XLS",
        ".pptx": "PPT",
        ".png": "IMG",
        ".jpg": "IMG",
        ".jpeg": "IMG",
        ".webp": "IMG",
        ".txt": "TXT",
        ".md": "MD",
        ".py": "PY",
    }.get(path.suffix.lower(), "FILE")


def _human_size(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "файл не найден"
    if size < 1024:
        return f"{size} Б"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} КБ"
    return f"{size / (1024 * 1024):.1f} МБ"


class PeopleEditor(QWidget):
    """Editable list of authors or reviewers without pipe-delimited fields."""

    changed = Signal()

    def __init__(self, title: str, people: list[Signatory] | None = None, parent=None):
        super().__init__(parent)
        self.rows: list[tuple[QLineEdit, QLineEdit, QLineEdit, QWidget]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        header = QHBoxLayout()
        header.addWidget(QLabel(title))
        header.addStretch(1)
        add = QToolButton()
        add.setText("+ Участник")
        add.setToolTip("Добавить участника: роль, имя и группа")
        add.clicked.connect(self.add_person)
        header.addWidget(add)
        layout.addLayout(header)
        self.rows_layout = QVBoxLayout()
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.rows_layout)
        for person in people or []:
            self.add_person(person)

    def add_person(self, person: Signatory | None = None):
        if isinstance(person, bool):  # clicked(bool) is not a person
            person = None
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        role = QLineEdit(person.role if person else "Автор")
        role.setPlaceholderText("Роль")
        role.setMaximumWidth(100)
        name = QLineEdit(person.name if person else "")
        name.setMinimumWidth(0)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        name.setPlaceholderText("Имя")
        group = QLineEdit(person.group if person else "")
        group.setPlaceholderText("Группа")
        group.setMaximumWidth(100)
        remove = QToolButton()
        remove.setText("×")
        remove.setToolTip("Убрать участника")
        layout.addWidget(role)
        layout.addWidget(name, 1)
        layout.addWidget(group)
        layout.addWidget(remove)
        self.rows.append((role, name, group, row))
        self.rows_layout.addWidget(row)
        for field in (role, name, group):
            field.textChanged.connect(self.changed.emit)
        remove.clicked.connect(lambda: self.remove_person(row))
        self.changed.emit()

    def remove_person(self, row: QWidget):
        self.rows = [entry for entry in self.rows if entry[3] is not row]
        self.rows_layout.removeWidget(row)
        row.deleteLater()
        self.changed.emit()

    def values(self) -> list[Signatory]:
        return [
            Signatory(
                name=name.text().strip(), role=role.text().strip() or "Автор", group=group.text().strip()
            )
            for role, name, group, _ in self.rows
            if name.text().strip()
        ]


class AuthorProfileDialog(QDialog):
    def __init__(self, profile: AuthorProfile, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Профиль автора по умолчанию")
        self.resize(480, 340)
        self.fields: dict[str, QLineEdit] = {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        for key, label in [
            ("organization", "Организация"),
            ("department", "Кафедра / подразделение"),
            ("author", "Автор"),
            ("group", "Группа"),
            ("supervisor", "Руководитель"),
            ("city", "Город"),
            ("year", "Год"),
        ]:
            field = QLineEdit(getattr(profile, key))
            self.fields[key] = field
            form.addRow(label, field)
        layout.addLayout(form)
        layout.addWidget(
            muted(
                "Эти значения будут подставляться в новые документы; их можно изменить отдельно в каждом проекте."
            )
        )
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self) -> AuthorProfile:
        return AuthorProfile(**{key: field.text().strip() for key, field in self.fields.items()})


class SourceDetailsDialog(QDialog):
    def __init__(self, source: DraftSource, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Свойства файла")
        self.resize(500, 260)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.reference = QLineEdit(source.reference)
        self.reference.setPlaceholderText("Библиографическая запись, если она известна")
        form.addRow("Библиография", self.reference)
        layout.addLayout(form)
        self.verified = QCheckBox("Библиографическая запись проверена")
        self.verified.setChecked(source.reference_verified)
        layout.addWidget(self.verified)
        self.confidential = QCheckBox("Пометить файл как конфиденциальный")
        self.confidential.setChecked(source.confidential)
        layout.addWidget(self.confidential)
        self.title_metadata = QCheckBox("Использовать для данных титульного листа")
        self.title_metadata.setEnabled(source.role == "requirements_style")
        self.title_metadata.setToolTip("Образец титула выбирается в корзине требований и оформления")
        self.title_metadata.setChecked(source.title_metadata)
        layout.addWidget(self.title_metadata)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MarkdownHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.set_theme(tokens_for())

    def set_theme(self, tokens: dict[str, str]):
        self.rules: list[tuple[str, QTextCharFormat]] = []
        for pattern, color, bold in [
            (r"^#{1,6}\s.*$", tokens["accent_foreground"], True),
            (r"`[^`]+`", tokens["bucket_context"], False),
            (r"\*\*[^*]+\*\*", tokens["bucket_style"], True),
            (r"\[[^\]]+\]\([^\)]+\)", tokens["bucket_citable"], False),
            (r"^\s*[-*+]\s+", tokens["bucket_context"], True),
        ]:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold:
                fmt.setFontWeight(QFont.Weight.Bold)
            self.rules.append((pattern, fmt))
        self.rehighlight()

    def highlightBlock(self, text: str):
        for pattern, fmt in self.rules:
            for match in re.finditer(pattern, text):
                self.setFormat(match.start(), match.end() - match.start(), fmt)


class FileCard(QWidget):
    def __init__(self, source: DraftSource, open_callback, details_callback, exclude_callback, parent=None):
        super().__init__(parent)
        path = Path(source.path)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 2, 5, 2)
        layout.setSpacing(6)
        icon = QLabel(_file_icon(path))
        icon.setObjectName("fileType")
        icon.setFixedWidth(36)
        layout.addWidget(icon)
        name = QLabel(source.name or path.name)
        name.setToolTip(str(path))
        name.setMinimumWidth(0)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(name, 1)
        size = QLabel(_human_size(path))
        size.setObjectName("muted")
        layout.addWidget(size)
        open_button = QToolButton()
        open_button.setText("↗")
        open_button.setToolTip("Открыть файл")
        open_button.clicked.connect(lambda: open_callback(source.id))
        details_button = QToolButton()
        details_button.setText("⋯")
        details_button.setToolTip("Библиография и приватность")
        details_button.clicked.connect(lambda: details_callback(source.id))
        remove_button = QToolButton()
        remove_button.setText("×")
        remove_button.setToolTip("Убрать из материалов (оригинал сохранится)")
        remove_button.clicked.connect(lambda: exclude_callback(source.id))
        layout.addWidget(open_button)
        layout.addWidget(details_button)
        layout.addWidget(remove_button)
        self.action_buttons = (open_button, details_button, remove_button)
        for action in self.action_buttons:
            action.setFixedSize(24, 26)
            action.setObjectName("fileAction")
        self.setFixedHeight(36)


class FileBucketList(QListWidget):
    def __init__(self, role: SourceRole, add_files_callback, move_callback, path_lookup, parent=None):
        super().__init__(parent)
        self.role = role
        self.add_files_callback = add_files_callback
        self.move_callback = move_callback
        self.path_lookup = path_lookup
        self.setObjectName("bucketList")
        self.setMinimumHeight(58)
        self.setMaximumHeight(160)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def startDrag(self, supportedActions):
        # The application owns the move. Qt's default MoveAction would remove
        # a row a second time after the destination has rebuilt both buckets.
        items = self.selectedItems()
        if not items:
            return
        drag = QDrag(self)
        drag.setMimeData(self.mimeData(items))
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, Qt.DropAction.MoveAction)

    def mimeData(self, items: list[QListWidgetItem]) -> QMimeData:
        mime = QMimeData()
        ids = [
            str(item.data(Qt.ItemDataRole.UserRole)) for item in items if item.data(Qt.ItemDataRole.UserRole)
        ]
        mime.setData(SOURCE_MIME_TYPE, QByteArray("\n".join(ids).encode("utf-8")))
        mime.setUrls(
            [
                QUrl.fromLocalFile(str(self.path_lookup(source_id)))
                for source_id in ids
                if self.path_lookup(source_id)
            ]
        )
        return mime

    def dropEvent(self, event):
        if event.mimeData().hasFormat(SOURCE_MIME_TYPE):
            raw = bytes(event.mimeData().data(SOURCE_MIME_TYPE)).decode("utf-8", errors="ignore")
            for source_id in raw.splitlines():
                self.move_callback(source_id, self.role)
            event.acceptProposedAction()
            return
        if event.mimeData().hasUrls():
            paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
            self.add_files_callback(self.role, paths)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasFormat(SOURCE_MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)


class WorkspaceTree(QTreeWidget):
    def startDrag(self, supportedActions):
        item = self.currentItem()
        value = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not value:
            return
        path = Path(value)
        if not path.exists():
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class HandoffBadge(QPushButton):
    """A real filesystem URL for Windows drag-out, with normal click to open."""

    def __init__(self, text, path_callback, parent=None):
        super().__init__(text, parent)
        self.path_callback = path_callback
        self.press_position = None
        self.setToolTip("Перетащите в окно внешнего агента или нажмите, чтобы открыть")
        self.clicked.connect(self.open_target)

    def open_target(self):
        path = self.path_callback()
        if path and path.exists():
            open_path(path)

    def mousePressEvent(self, event):
        self.press_position = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not event.buttons() & Qt.MouseButton.LeftButton or self.press_position is None:
            return
        if (
            event.position().toPoint() - self.press_position
        ).manhattanLength() < QApplication.startDragDistance():
            return
        path = self.path_callback()
        if not path or not path.exists():
            return
        self.setDown(False)
        self.press_position = None
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class SummaryButton(QPushButton):
    """Keep long title metadata from imposing a minimum editor width."""

    def setText(self, text):
        self.full_text = text
        self.setToolTip(text)
        super().setText(
            self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, max(40, self.width() - 24))
        )

    def minimumSizeHint(self):
        return QSize(80, 34)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setText(getattr(self, "full_text", "Титульные данные"))


class WorkspacePathLabel(QLabel):
    """Show both ends of a path without a line edit's horizontal scrolling."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.full_path = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumHeight(32)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def setText(self, text):
        self.full_path = text
        self.setToolTip(text)
        self._update_text()

    def _update_text(self):
        super().setText(
            self.fontMetrics().elidedText(
                self.full_path, Qt.TextElideMode.ElideMiddle, max(0, self.contentsRect().width())
            )
        )

    def minimumSizeHint(self):
        return QSize(0, 32)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_text()


class ProjectItemDelegate(QStyledItemDelegate):
    """Keep titles neutral while coloring just the lifecycle line."""

    def __init__(self, tokens_callback, parent=None):
        super().__init__(parent)
        self.tokens_callback = tokens_callback

    def paint(self, painter, option, index):
        # PySide's stubs omit the public data fields of QStyleOptionViewItem.
        opt: Any = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        lines = opt.text.splitlines()
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        tokens = self.tokens_callback()
        painter.save()
        content = opt.rect.adjusted(11, 8, -8, -6)
        painter.setClipRect(content)
        font = QFont(opt.font)
        y = content.top()
        for number, line in enumerate(lines[:3]):
            if number:
                font.setPointSizeF(max(8, opt.font.pointSizeF() - 1))
            painter.setFont(font)
            metrics = painter.fontMetrics()
            color = (
                tokens[status_tone(line)]
                if number == 2
                else tokens["text_primary" if number == 0 else "text_secondary"]
            )
            painter.setPen(QColor(color))
            painter.drawText(
                content.left(),
                y + metrics.ascent(),
                metrics.elidedText(line, Qt.TextElideMode.ElideRight, content.width()),
            )
            y += metrics.height() + 3
        painter.restore()


class MainWindow(QMainWindow):
    def __init__(self, library: Library | None = None):
        super().__init__()
        self.library = library or Library()
        self.settings_path = self.library.directory / "settings.json"
        self.settings = read_json(self.settings_path) if self.settings_path.exists() else {}
        self.pool = QThreadPool.globalInstance()
        self.worker = None
        self.result_callback = None
        self.draft: WorkspaceDraft | None = None
        self.current_workspace: Path | None = None
        self.attachments: dict[str, DraftSource] = {}
        self.excluded: dict[str, DraftExcludedSource] = {}
        self.templates: list[Template] = []
        self.profiles: list[AgentProfile] = []
        self.template_list: QListWidget | None = None
        self.loading = False
        self.title_sample: Path | None = None
        self._observed_stamp = None
        self._build_assessments: dict[str, tuple] = {}
        self.setWindowTitle("CodexDoc · Рабочее пространство")
        self.resize(1440, 900)
        self.setMinimumSize(1100, 720)
        self._build_ui()
        application = QApplication.instance()
        assert isinstance(application, QApplication)
        self.theme = ThemeController(application, self.settings.get("theme_mode", "auto"), self)
        self.theme.changed.connect(self.theme_updated)
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(self.theme.mode))
        self.theme_combo.currentIndexChanged.connect(self.change_theme)
        self.project_list.setItemDelegate(ProjectItemDelegate(lambda: self.theme.tokens, self.project_list))
        self.theme_updated()
        self._connect_autosave()
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(450)
        self._autosave_timer.timeout.connect(self.persist_current_draft)
        self.refresh_library()
        drafts = self.library.drafts()
        last = next((draft for draft in drafts if draft.id == self.settings.get("active_draft")), None)
        if last:
            if (
                last.workspace_path
                and (Path(last.workspace_path) / "workspace.json").is_file()
                and not last.pending_changes
            ):
                self._load_workspace(Path(last.workspace_path))
            else:
                self._load_draft(last)
            self.refresh_project_list(last.id)
        else:
            self.new_document()
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(2500)
        self._watch_timer.timeout.connect(self.refresh_external_changes)
        self._watch_timer.start()
        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self.save_workspace)
        QShortcut(QKeySequence("Ctrl+B"), self).activated.connect(lambda: self.run_pipeline("build"))
        QShortcut(QKeySequence("Ctrl+N"), self).activated.connect(self.new_document)

    def _build_ui(self):
        root = QWidget(self)
        root.setObjectName("studioRoot")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(8, 8, 8, 4)
        root_layout.setSpacing(4)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        root_layout.addWidget(self.main_splitter, 1)
        self._build_sidebar()
        self._build_editor()
        self._build_inspector()
        self.main_splitter.addWidget(self.sidebar)
        self.main_splitter.addWidget(self.editor_scroll)
        self.main_splitter.addWidget(self.inspector)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setStretchFactor(2, 0)
        self.main_splitter.setSizes([248, 800, 368])
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        root_layout.addWidget(self.progress)
        self.status = QLabel(
            "Все материалы остаются на этом компьютере. Внешнего агента запускает пользователь."
        )
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        root_layout.addWidget(self.status)

    def _build_sidebar(self):
        self.sidebar = QWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setMinimumWidth(240)
        self.sidebar.setMaximumWidth(260)
        layout = QVBoxLayout(self.sidebar)
        layout.setContentsMargins(7, 10, 7, 8)
        layout.setSpacing(8)
        brand = QLabel("CodexDoc")
        brand.setObjectName("brand")
        layout.addWidget(brand)
        layout.addWidget(muted("Локальная студия документов"))
        new_button = button("+ Новый документ", self.new_document, True)
        new_button.setMinimumHeight(38)
        layout.addWidget(new_button)
        layout.addWidget(button("Открыть проект…", self.open_project))
        heading = QLabel("Проекты")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        self.project_search = QLineEdit()
        self.project_search.setPlaceholderText("Найти проект…")
        self.project_search.textChanged.connect(self.filter_projects)
        layout.addWidget(self.project_search)
        self.project_list = QListWidget()
        self.project_list.setObjectName("projectList")
        self.project_list.currentItemChanged.connect(self.select_project)
        layout.addWidget(self.project_list, 1)
        layout.addWidget(muted("Черновики сохраняются автоматически."))
        footer = QFrame()
        footer.setObjectName("sidebarFooter")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(0, 8, 0, 0)
        author_button = button("Профиль автора ⚙", self.edit_author_profile)
        library_button = button("Библиотека шаблонов", self.show_template_library)
        footer_layout.addWidget(author_button)
        footer_layout.addWidget(library_button)
        self.help_button = button("Как работать ?", self.open_user_guide)
        self.help_button.setToolTip("Открыть локальное руководство в браузере")
        footer_layout.addWidget(self.help_button)
        theme_row = QHBoxLayout()
        theme_row.addWidget(muted("Тема:"))
        self.theme_combo = QComboBox()
        self.theme_combo.setAccessibleName("Тема приложения")
        for label, mode in (("Авто", "auto"), ("Светлая", "light"), ("Тёмная", "dark")):
            self.theme_combo.addItem(label, mode)
        theme_row.addWidget(self.theme_combo, 1)
        footer_layout.addLayout(theme_row)
        layout.addWidget(footer)

    def open_user_guide(self):
        try:
            open_path(ROOT / "Как работать.html")
        except ValueError as exc:
            self.show_error(str(exc))

    def change_theme(self):
        mode = self.theme_combo.currentData()
        self.settings["theme_mode"] = mode
        write_json(self.settings_path, self.settings)
        self.theme.set_mode(mode)

    def theme_updated(self):
        self.preview_highlighter.set_theme(self.theme.tokens)
        self.project_list.viewport().update()

    @staticmethod
    def color_status(label: QLabel):
        label.setProperty("tone", status_tone(label.text()))
        label.style().unpolish(label)
        label.style().polish(label)

    def _build_editor(self):
        self.editor_scroll = QScrollArea()
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.editor_scroll.setMinimumWidth(460)
        self.editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page = QWidget()
        page.setObjectName("editorPage")
        self.editor_page = page
        self.editor_scroll.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(12)
        eyebrow = QHBoxLayout()
        phase = QLabel("01 / КОНТЕКСТ И ПРАВИЛА")
        phase.setObjectName("eyebrow")
        eyebrow.addWidget(phase)
        eyebrow.addStretch()
        self.project_status = QLabel("Черновик")
        self.project_status.setObjectName("statusBadge")
        self.project_status.setMinimumWidth(130)
        self.project_status.setMaximumWidth(175)
        self.project_status.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        eyebrow.addWidget(self.project_status)
        layout.addLayout(eyebrow)
        header = QHBoxLayout()
        self.title_input = QLineEdit()
        self.title_input.setObjectName("documentTitle")
        self.title_input.setPlaceholderText("Название работы")
        self.title_input.setMinimumHeight(40)
        header.addWidget(self.title_input, 1)
        layout.addLayout(header)

        controls = QHBoxLayout()
        self.template_combo = QComboBox()
        self.template_combo.setMinimumWidth(0)
        self.template_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.template_combo.setToolTip("Шаблон оформления")
        controls.addWidget(self.template_combo, 1)
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(0)
        self.profile_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.profile_combo.setToolTip("Профиль агента")
        controls.addWidget(self.profile_combo, 1)
        help_button = QToolButton()
        help_button.setText("?")
        help_button.setToolTip(
            "Профиль задаёт автономность агента, режим проверки и правила работы с интернетом и ошибками."
        )
        controls.addWidget(help_button)
        configure = button("⚙", self.edit_agent_profile)
        configure.setToolTip("Настроить профиль агента")
        controls.addWidget(configure)
        layout.addLayout(controls)

        self.metadata_toggle = SummaryButton()
        self.metadata_toggle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.metadata_toggle.setObjectName("metadataToggle")
        self.metadata_toggle.setCheckable(True)
        self.metadata_toggle.clicked.connect(self.toggle_metadata)
        layout.addWidget(self.metadata_toggle)
        self.metadata_frame = QFrame()
        self.metadata_frame.setObjectName("surface")
        metadata_layout = QVBoxLayout(self.metadata_frame)
        metadata_layout.setContentsMargins(12, 10, 12, 10)
        metadata_form = QGridLayout()
        self.metadata_grid = metadata_form
        self.metadata_cells: list[QWidget] = []
        self.metadata_fields: dict[str, QLineEdit] = {}
        for key, label in [
            ("organization", "Организация"),
            ("department", "Кафедра / подразделение"),
            ("author", "Автор"),
            ("group", "Группа"),
            ("supervisor", "Руководитель"),
            ("city", "Город"),
            ("year", "Год"),
        ]:
            field = QLineEdit()
            self.metadata_fields[key] = field
            field.textChanged.connect(self._update_metadata_summary)
            index = len(self.metadata_fields) - 1
            cell_widget = QWidget()
            cell = QVBoxLayout(cell_widget)
            cell.setContentsMargins(0, 0, 0, 0)
            cell.setSpacing(4)
            cell.addWidget(muted(label))
            cell.addWidget(field)
            metadata_form.addWidget(cell_widget, index // 2, index % 2)
            self.metadata_cells.append(cell_widget)
        metadata_layout.addLayout(metadata_form)
        self.people_fields = {
            "authors": PeopleEditor("Соавторы"),
            "reviewers": PeopleEditor("Проверяющие"),
        }
        for editor in self.people_fields.values():
            editor.changed.connect(self.schedule_autosave)
            metadata_layout.addWidget(editor)
        self.metadata_frame.hide()
        layout.addWidget(self.metadata_frame)

        req_header = QHBoxLayout()
        req_label = QLabel("Задание и требования")
        req_label.setObjectName("sectionTitle")
        req_header.addWidget(req_label)
        req_header.addStretch(1)
        self.requirements_focus_button = button("Фокус ↗", self.toggle_requirements_focus)
        req_header.addWidget(self.requirements_focus_button)
        layout.addLayout(req_header)
        self.requirements = QPlainTextEdit()
        self.requirements.setObjectName("requirementsInput")
        self.requirements.setPlaceholderText(
            "Опишите задание своими словами.\n\nТема и цель работы\nСтруктура разделов\nОбъём и требования преподавателя"
        )
        self.requirements.setMinimumHeight(185)
        self.requirements.setMaximumHeight(260)
        layout.addWidget(self.requirements)

        materials_header = QHBoxLayout()
        materials_title = QLabel("Материалы")
        materials_title.setObjectName("sectionTitle")
        materials_header.addWidget(materials_title)
        materials_header.addStretch(1)
        layout.addLayout(materials_header)
        self.bucket_lists: dict[SourceRole, FileBucketList] = {}
        self.bucket_frames: dict[SourceRole, QFrame] = {}
        self.bucket_empty_text: dict[SourceRole, str] = {
            "citable": "Добавьте источники, на которые агент может ссылаться.",
            "context_only": "Добавьте контекст, который нельзя упоминать или цитировать.",
            "requirements_style": "Добавьте методичку или образец оформления.",
        }
        for role in ("citable", "context_only", "requirements_style"):
            frame = QFrame()
            frame.setObjectName("bucketCard")
            frame.setProperty("bucketRole", role)
            frame_layout = QVBoxLayout(frame)
            frame_layout.setContentsMargins(10, 7, 8, 7)
            frame_layout.setSpacing(5)
            title_row = QHBoxLayout()
            title = QLabel(SOURCE_ROLE_LABELS[role])
            title.setObjectName("sectionTitle")
            title.setWordWrap(True)
            title_row.addWidget(title)
            title_row.addStretch(1)
            add_button = button("+ Добавить", partial(self.add_files_dialog, role))
            title_row.addWidget(add_button)
            frame_layout.addLayout(title_row)
            file_list = FileBucketList(role, self.add_files, self.move_attachment, self.attachment_path)
            frame_layout.addWidget(file_list)
            if role == "requirements_style":
                self.extract_title_button = button(
                    "Извлечь титульные данные\nиз образца", self.extract_title_sample
                )
                self.extract_title_button.setVisible(False)
                frame_layout.addWidget(self.extract_title_button)
            self.bucket_lists[role] = file_list
            self.bucket_frames[role] = frame
            layout.addWidget(frame)

        self.title_sample_label = QLabel("Образец титула не выбран")
        self.title_sample_label.setObjectName("muted")
        self.title_sample_label.setWordWrap(True)
        layout.addWidget(self.title_sample_label)
        self.clear_title_sample_button = button("Убрать образец", lambda: self.set_title_sample(None))
        self.clear_title_sample_button.hide()
        layout.addWidget(self.clear_title_sample_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self.excluded_toggle = QToolButton()
        self.excluded_toggle.setText("Исключённые файлы")
        self.excluded_toggle.setCheckable(True)
        self.excluded_toggle.clicked.connect(lambda checked: self.excluded_list.setVisible(checked))
        self.excluded_list = QListWidget()
        self.excluded_list.setMaximumHeight(130)
        self.excluded_list.hide()
        self.excluded_toggle.hide()
        layout.addWidget(self.excluded_toggle)
        layout.addWidget(self.excluded_list)
        layout.addStretch(1)

    def _build_inspector(self):
        self.inspector = QWidget()
        self.inspector.setObjectName("inspector")
        self.inspector.setMinimumWidth(340)
        self.inspector.setMaximumWidth(400)
        layout = QVBoxLayout(self.inspector)
        layout.setContentsMargins(6, 6, 6, 4)
        layout.setSpacing(8)
        title = QLabel("02 / ПЕРЕДАЧА И СБОРКА")
        title.setObjectName("eyebrow")
        layout.addWidget(title)
        path_card = QFrame()
        path_card.setObjectName("inspectorSurface")
        path_layout = QVBoxLayout(path_card)
        path_layout.setContentsMargins(9, 8, 9, 8)
        path_title = QLabel("Путь сохранения")
        path_title.setObjectName("sectionTitle")
        path_layout.addWidget(path_title)
        path_row = QHBoxLayout()
        self.parent_input = WorkspacePathLabel()
        self.parent_input.setObjectName("workspacePath")
        path_row.addWidget(self.parent_input, 1)
        self.change_path_button = button("Изменить…", self.choose_parent)
        path_row.addWidget(self.change_path_button)
        path_layout.addLayout(path_row)
        self.open_folder_button = button("Открыть в Проводнике", self.open_workspace_folder)
        path_layout.addWidget(self.open_folder_button)
        layout.addWidget(path_card)

        self.inspector_tabs = QTabWidget()
        handoff = QWidget()
        handoff_layout = QVBoxLayout(handoff)
        handoff_layout.setContentsMargins(8, 8, 8, 8)
        handoff_layout.addWidget(QLabel("Структура рабочей папки"))
        self.workspace_tree = WorkspaceTree()
        self.workspace_tree.setHeaderHidden(True)
        self.workspace_tree.setDragEnabled(True)
        self.workspace_tree.setMinimumHeight(110)
        self.workspace_tree.setMaximumHeight(230)
        self.workspace_tree.currentItemChanged.connect(self.preview_tree_item)
        handoff_layout.addWidget(self.workspace_tree, 2)
        self.preview_caption = QLabel("Предпросмотр сгенерированных инструкций")
        self.preview_caption.setObjectName("muted")
        self.preview_caption.setWordWrap(True)
        handoff_layout.addWidget(self.preview_caption)
        self.preview_text = QPlainTextEdit()
        self.preview_text.setObjectName("previewText")
        self.preview_text.setReadOnly(True)
        self.preview_text.setMinimumHeight(120)
        self.preview_highlighter = MarkdownHighlighter(self.preview_text.document())
        handoff_layout.addWidget(self.preview_text, 3)
        badges = QHBoxLayout()
        self.instructions_badge = HandoffBadge("INSTRUCTIONS.md ↗", self.instructions_path)
        self.folder_badge = HandoffBadge("Рабочая папка ↗", lambda: self.current_workspace)
        badges.addWidget(self.instructions_badge)
        badges.addWidget(self.folder_badge)
        handoff_layout.addLayout(badges)
        handoff_layout.addWidget(button("Копировать стартовую команду", self.copy_start_command))
        handoff_layout.addWidget(button("Копировать текст инструкций", self.copy_instructions))
        self.inspector_tabs.addTab(handoff, "Передача агенту")

        build_page = QWidget()
        build_layout = QVBoxLayout(build_page)
        build_layout.setContentsMargins(8, 8, 8, 8)
        self.build_summary = QLabel("Сборка ещё не выполнялась")
        self.build_summary.setObjectName("sectionTitle")
        self.build_summary.setWordWrap(True)
        build_layout.addWidget(self.build_summary)
        self.validation_labels: dict[str, QLabel] = {}
        for key in ("fields", "context", "format", "review"):
            label = QLabel()
            label.setWordWrap(True)
            label.setObjectName("muted")
            self.validation_labels[key] = label
            build_layout.addWidget(label)
        self.report_text = QPlainTextEdit()
        self.report_text.setReadOnly(True)
        self.report_text.setObjectName("previewText")
        build_layout.addWidget(self.report_text, 1)
        row = QHBoxLayout()
        row.addWidget(button("Проверить", lambda: self.run_pipeline("check")))
        row.addWidget(button("Просмотреть страницы", self.show_review))
        build_layout.addLayout(row)
        outputs = QHBoxLayout()
        self.docx_button = button("Открыть DOCX", lambda: self.open_current("output/document.docx"))
        self.pdf_button = button("Открыть PDF", lambda: self.open_current("output/document.pdf"))
        outputs.addWidget(self.docx_button)
        outputs.addWidget(self.pdf_button)
        build_layout.addLayout(outputs)
        self.publish_button = button("Сохранить проверенный шаблон в библиотеку", self.publish_current)
        build_layout.addWidget(self.publish_button)
        self.inspector_tabs.addTab(build_page, "Сборка / Проверка")
        layout.addWidget(self.inspector_tabs, 1)

        footer = QFrame()
        footer.setObjectName("inspectorSurface")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(7, 7, 7, 7)
        self.dirty_label = muted("Черновик сохраняется автоматически")
        self.dirty_label.setWordWrap(True)
        footer_layout.addWidget(self.dirty_label)
        action_row = QHBoxLayout()
        self.prepare_button = button("1. Подготовить\nпапку", self.save_workspace, True)
        self.prepare_button.setMinimumHeight(48)
        self.save_button = self.prepare_button
        self.build_button = button("2. Собрать\nDOCX и PDF", lambda: self.run_pipeline("build"))
        self.build_button.setMinimumHeight(48)
        action_row.addWidget(self.prepare_button, 1)
        action_row.addWidget(self.build_button, 1)
        footer_layout.addLayout(action_row)
        layout.addWidget(footer)

    def _connect_autosave(self):
        for field in [self.title_input, self.requirements, *self.metadata_fields.values()]:
            signal = field.textChanged
            signal.connect(self.schedule_autosave)
        self.template_combo.currentIndexChanged.connect(self.schedule_autosave)
        self.profile_combo.currentIndexChanged.connect(self.schedule_autosave)

    def refresh_library(self):
        self.templates = self.library.templates()
        self.profiles = self.library.profiles()
        template_id = self.draft.template_id if self.draft else self.settings.get("template", "report")
        profile_id = self.draft.profile_id if self.draft else self.settings.get("profile", "balanced")
        self.loading = True
        self.template_combo.clear()
        self.profile_combo.clear()
        for template in self.templates:
            self.template_combo.addItem(template.name, template.id)
        if self.draft and self.draft.template_snapshot and self.template_combo.findData(template_id) < 0:
            self.template_combo.addItem(self.draft.template_snapshot.name + " · копия проекта", template_id)
            self.templates.append(self.draft.template_snapshot)
        for profile in self.profiles:
            self.profile_combo.addItem(profile.name, profile.id)
        if self.draft and self.draft.profile_snapshot and self.profile_combo.findData(profile_id) < 0:
            self.profile_combo.addItem(self.draft.profile_snapshot.name + " · копия проекта", profile_id)
            self.profiles.append(self.draft.profile_snapshot)
        self.template_combo.setCurrentIndex(max(0, self.template_combo.findData(template_id)))
        self.profile_combo.setCurrentIndex(max(0, self.profile_combo.findData(profile_id)))
        self.loading = False

    def selected_template(self) -> Template:
        template_id = self.template_combo.currentData()
        if self.draft and template_id == self.draft.template_id and self.draft.template_snapshot:
            return self.draft.template_snapshot.model_copy(deep=True)
        return self.library.get_template(template_id)

    def selected_profile(self) -> AgentProfile:
        profile_id = self.profile_combo.currentData()
        if self.draft and profile_id == self.draft.profile_id and self.draft.profile_snapshot:
            return self.draft.profile_snapshot.model_copy(deep=True)
        return next(profile for profile in self.profiles if profile.id == profile_id).model_copy(deep=True)

    def _default_metadata(self) -> Metadata:
        profile = self.library.author_profile()
        if not profile.year:
            profile.year = str(date.today().year)
        return Metadata(**profile.model_dump(mode="python"))

    def new_document(self):
        if self.worker is not None or not self.persist_current_draft():
            return
        metadata = self._default_metadata()
        template = self.library.get_template(self.settings.get("template", "report"))
        profile = next(
            (item for item in self.library.profiles() if item.id == self.settings.get("profile", "balanced")),
            AgentProfile(),
        )
        draft = WorkspaceDraft(
            metadata=metadata,
            template_id=template.id,
            template_snapshot=template,
            profile_id=profile.id,
            profile_snapshot=profile,
            parent_path=self.settings.get("workspace_parent", str(WORKSPACES)),
            auto_title=True,
        )
        self.library.save_draft(draft)
        self._load_draft(draft)
        self.refresh_project_list(draft.id)

    def _load_draft(self, draft: WorkspaceDraft):
        self._autosave_timer.stop()
        self.loading = True
        self.draft = draft.model_copy(deep=True)
        self.current_workspace = Path(draft.workspace_path) if draft.workspace_path else None
        self.attachments = {source.id: source.model_copy(deep=True) for source in draft.sources}
        self.excluded = {source.id: source.model_copy(deep=True) for source in draft.excluded_sources}
        self.title_input.setText("" if draft.auto_title else draft.metadata.title)
        self.requirements.setPlainText(draft.requirements)
        self.parent_input.setText(self._display_path())
        for key, field in self.metadata_fields.items():
            field.setText(getattr(draft.metadata, key))
        for key, editor in self.people_fields.items():
            while editor.rows:
                editor.remove_person(editor.rows[-1][3])
            for person in getattr(draft.metadata, key):
                editor.add_person(person)
        self.refresh_library()
        self.title_sample = next(
            (Path(source.path) for source in self.attachments.values() if source.title_metadata), None
        )
        self._refresh_title_sample_label()
        self.metadata_frame.hide()
        self.metadata_toggle.setChecked(False)
        self.loading = False
        self._render_buckets()
        self._render_excluded()
        self._refresh_inspector()
        self._refresh_status()
        self._refresh_report()
        self._update_metadata_summary()
        self.settings["active_draft"] = draft.id
        write_json(self.settings_path, self.settings)
        self._observed_stamp = self.workspace_stamp()
        self.build_button.setEnabled(self.current_workspace is not None and not self.draft.pending_changes)

    def _capture_draft(self):
        if self.draft is None or self.loading:
            return
        template = self.selected_template()
        profile = self.selected_profile()
        metadata_values = {key: field.text().strip() for key, field in self.metadata_fields.items()}
        self.draft.metadata = Metadata(
            title=self.title_input.text().strip() or self.default_title(),
            kind=template.name.split(" · ")[0],
            authors=self.people_fields["authors"].values(),
            reviewers=self.people_fields["reviewers"].values(),
            **metadata_values,
        )
        self.draft.auto_title = not bool(self.title_input.text().strip())
        self.draft.requirements = self.requirements.toPlainText().strip()
        self.draft.template_id = template.id
        self.draft.template_snapshot = template
        self.draft.profile_id = profile.id
        self.draft.profile_snapshot = profile
        self.draft.sources = [source.model_copy(deep=True) for source in self.attachments.values()]
        self.draft.excluded_sources = [source.model_copy(deep=True) for source in self.excluded.values()]

    def schedule_autosave(self, *_args):
        if self.loading or self.draft is None:
            return
        self.draft.pending_changes = True
        self.project_status.setText("Папка не обновлена" if self.current_workspace else "Черновик")
        self.color_status(self.project_status)
        self.build_button.setEnabled(False)
        self.dirty_label.setText("● Папка не обновлена")
        self._autosave_timer.start()
        # Preview follows the editor synchronously; disk persistence remains
        # debounced and must not hold up file-role or profile feedback.
        self._refresh_inspector()

    def persist_current_draft(self) -> bool:
        if self.draft is None:
            return True
        self._autosave_timer.stop()
        try:
            self._capture_draft()
            self.library.save_draft(self.draft)
            self.parent_input.setText(self._display_path())
            self._refresh_inspector()
            self._refresh_status()
            self.refresh_project_list()
            return True
        except (ValueError, OSError) as exc:
            self.status.setText(f"Черновик пока не сохранён: {exc}")
            return False

    def default_title(self) -> str:
        return f"{self.selected_template().name.split(' · ')[0]} {date.today():%Y-%m-%d}"

    def _display_path(self) -> str:
        if self.current_workspace:
            return str(self.current_workspace)
        parent = Path(self.draft.parent_path) if self.draft and self.draft.parent_path else WORKSPACES
        title = self.title_input.text().strip() or self.default_title()
        return str(parent / safe_name(title))

    def _refresh_status(self):
        if self.draft is None:
            self.project_status.setText("Черновик")
            return
        project_status = self._project_status(self.current_workspace, self.draft)
        self.project_status.setText(project_status)
        self.color_status(self.project_status)
        self.project_status.setToolTip(project_status)
        dirty = self.draft.pending_changes
        self.dirty_label.setText(
            "● Папка не обновлена"
            if dirty
            else "Папка и инструкции актуальны"
            if self.current_workspace
            else "Черновик сохраняется автоматически"
        )
        self.profile_combo.setToolTip(self.profile_description())
        self.prepare_button.setObjectName("primary" if dirty or not self.current_workspace else "")
        self.build_button.setObjectName("primary" if self.current_workspace and not dirty else "")
        for control in (self.prepare_button, self.build_button):
            control.style().unpolish(control)
            control.style().polish(control)

    def profile_description(self) -> str:
        profile = self.selected_profile()
        autonomy = {
            "guided": "Согласует существенные решения",
            "balanced": "Уточняет существенные пробелы",
            "autonomous": "Самостоятельно выполняет задание",
        }
        internet = {
            "off": "Интернет запрещён",
            "ask": "Интернет с разрешения",
            "allowed": "Интернет разрешён",
        }
        return (
            f"{autonomy[profile.autonomy]}. {internet[profile.internet]}. Проверка: {profile.verification}."
        )

    def _project_status(self, workspace_path: Path | None, draft: WorkspaceDraft | None = None) -> str:
        if draft and draft.pending_changes and workspace_path:
            return "Папка не обновлена"
        if not workspace_path or not (workspace_path / "workspace.json").is_file():
            return "Черновик"
        report_model, stale = self.assess_build(workspace_path)
        report = report_model.model_dump() if report_model else {}
        pdf_exists = (workspace_path / "output" / "document.pdf").is_file()
        status = report.get("status")
        if stale and report_model is None and (workspace_path / "output/checks.json").exists():
            return "Требует проверки ⚠"
        if stale and report.get("artifact_hashes"):
            return "Требует пересборки ⚠"
        if status == "blocked" or any(
            item.get("severity") == "warning" for item in report.get("findings", [])
        ):
            return "Есть предупреждения ⚠"
        if status == "preview" and pdf_exists:
            return "Предпросмотр PDF"
        if pdf_exists and status == "ready":
            return "Собран PDF ✓"
        if pdf_exists:
            return "Собран PDF · нужна проверка"
        return (
            "Готов к агенту"
            if self._instructions_are_current(workspace_path)
            else "Инструкции требуют обновления ⚠"
        )

    def assess_build(self, path: Path):
        stamp = self.workspace_stamp(path)
        key = str(path.resolve())
        cached = self._build_assessments.get(key)
        if cached is None or cached[0] != stamp:
            cached = (stamp, current_build(path))
            self._build_assessments[key] = cached
        return cached[1]

    def workspace_stamp(self, path: Path | None = None):
        path = path or self.current_workspace
        if path is None:
            return ()
        candidates = list(path.glob("*.json")) + list(path.glob("*.md"))
        for folder in ("sources", "context", "template", "norms", "assets", "analysis", "output", ".cache"):
            candidates.extend((path / folder).rglob("*"))
        candidates.extend(Path(__file__).parent.glob("*.py"))
        result = []
        for candidate in candidates:
            try:
                if candidate.is_file():
                    stat = candidate.stat()
                    result.append((str(candidate), stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
        return tuple(sorted(result))

    def refresh_external_changes(self):
        if self.worker is not None or not self.current_workspace or self.loading:
            return
        stamp = self.workspace_stamp()
        if stamp == self._observed_stamp:
            return
        self._observed_stamp = stamp
        try:
            if self.draft and not self.draft.pending_changes:
                self._load_workspace(self.current_workspace)
                self.status.setText("Изменения внешнего агента загружены. Актуальность сборки проверена.")
            else:
                self._refresh_report()
                self._refresh_status()
                self.status.setText(
                    "Файлы проекта изменились на диске. Черновик сохранён отдельно; проверьте изменения перед подготовкой папки."
                )
        except (ValueError, OSError) as exc:
            self._refresh_report()
            self.project_status.setText("Ошибка файлов ⚠")
            self.color_status(self.project_status)
            self.status.setText(f"Изменённые файлы пока не удалось прочитать: {exc}")

    @staticmethod
    def _instructions_are_current(workspace_path: Path) -> bool:
        try:
            manifest, template, profile, _ = load_workspace(workspace_path)
            expected = generate_agents(manifest, template, profile, workspace_path)
        except (OSError, ValueError):
            return False
        candidates = [
            workspace_path / "INSTRUCTIONS.md",
            workspace_path / "AGENTS.md",
            *sorted(workspace_path.glob("AGENTS.codexdoc-*.md")),
        ]
        for candidate in candidates:
            try:
                if candidate.is_file() and candidate.read_text(encoding="utf-8") == expected:
                    return True
            except OSError:
                continue
        return False

    def refresh_project_list(self, select_key: str | None = None):
        if not hasattr(self, "project_list"):
            return
        current_key = select_key or (self.draft.id if self.draft else "")
        drafts = self.library.drafts()
        by_path = {
            str(Path(draft.workspace_path).resolve()).casefold(): draft
            for draft in drafts
            if draft.workspace_path
        }
        paths = list(self.settings.get("recent", []))
        parent = Path(self.settings.get("workspace_parent", str(WORKSPACES)))
        if parent.is_dir():
            paths.extend(str(item.parent.resolve()) for item in parent.glob("*/workspace.json"))
        unique_paths: list[Path] = []
        seen: set[str] = set()
        for raw in paths:
            candidate = Path(raw)
            key = str(candidate.resolve()).casefold()
            if key not in seen and (candidate / "workspace.json").is_file():
                unique_paths.append(candidate)
                seen.add(key)
        records = []
        for path in unique_paths:
            draft = by_path.get(str(path.resolve()).casefold())
            try:
                workspace_name = (
                    draft.metadata.title
                    if draft
                    else read_json(path / "workspace.json").get("name", path.name)
                )
            except (OSError, ValueError):
                workspace_name = path.name
            records.append(
                {
                    "key": str(path.resolve()),
                    "kind": "workspace",
                    "path": str(path.resolve()),
                    "draft_id": draft.id if draft else "",
                    "title": workspace_name,
                    "status": self._project_status(path, draft),
                    "template": (
                        draft.template_snapshot.name
                        if draft and draft.template_snapshot
                        else read_json(path / "template.json").get("name", "Шаблон")
                    ),
                }
            )
        for draft in drafts:
            if draft.workspace_path and str(Path(draft.workspace_path).resolve()).casefold() in seen:
                continue
            records.append(
                {
                    "key": draft.id,
                    "kind": "draft",
                    "path": draft.workspace_path,
                    "draft_id": draft.id,
                    "title": draft.metadata.title,
                    "status": self._project_status(
                        Path(draft.workspace_path) if draft.workspace_path else None, draft
                    ),
                    "template": draft.template_snapshot.name
                    if draft.template_snapshot
                    else draft.template_id,
                }
            )

        self.loading = True
        self.project_list.clear()
        for record in records:
            item = QListWidgetItem(f"{record['title']}\n{record['template']}\n{record['status']}")
            item.setSizeHint(QSize(200, 78))
            item.setData(Qt.ItemDataRole.UserRole, record)
            item.setToolTip(record["path"] or record["title"])
            self.project_list.addItem(item)
            if record["key"] == current_key or record["draft_id"] == current_key:
                self.project_list.setCurrentItem(item)
        self.loading = False
        self.filter_projects(self.project_search.text())

    def filter_projects(self, text: str):
        for index in range(self.project_list.count()):
            item = self.project_list.item(index)
            item.setHidden(text.casefold() not in item.text().casefold())

    def open_project(self):
        selected = QFileDialog.getExistingDirectory(self, "Открыть рабочую папку CodexDoc", str(WORKSPACES))
        if not selected or not self.persist_current_draft():
            return
        try:
            path = Path(selected)
            pending = next(
                (
                    draft
                    for draft in self.library.drafts()
                    if draft.workspace_path
                    and Path(draft.workspace_path).resolve() == path.resolve()
                    and draft.pending_changes
                ),
                None,
            )
            if pending:
                self._load_draft(pending)
                self.refresh_project_list(pending.id)
            else:
                self._load_workspace(path)
        except (ValueError, OSError) as exc:
            self.show_error(str(exc))

    def select_project(self, item, _previous=None):
        if self.loading or item is None:
            return
        record = item.data(Qt.ItemDataRole.UserRole)
        if self.worker is not None or not self.persist_current_draft():
            self.refresh_project_list()
            return
        if not isinstance(record, dict):
            return
        draft = next((value for value in self.library.drafts() if value.id == record.get("draft_id")), None)
        if record["kind"] == "workspace":
            path = Path(record["path"])
            if draft is not None and draft.pending_changes:
                self._load_draft(draft)
            else:
                self._load_workspace(path)
        elif draft is not None:
            self._load_draft(draft)

    def _load_workspace(self, path: Path):
        manifest, template, profile, content = load_workspace(path)
        existing = next(
            (
                draft
                for draft in self.library.drafts()
                if draft.workspace_path and Path(draft.workspace_path).resolve() == path.resolve()
            ),
            None,
        )
        sources = [
            DraftSource(
                source_id=source.id,
                name=source.name,
                path=str((path / source.path).resolve()) if source.path else "",
                role=source.role,
                reference=source.reference,
                reference_verified=source.reference_verified,
                confidential=source.confidential,
                title_metadata=source.title_metadata,
            )
            for source in manifest.sources
        ]
        excluded = [
            DraftExcludedSource(
                source_id=source.id,
                name=source.name,
                confidential=source.confidential,
            )
            for source in manifest.excluded_sources
        ]
        if existing:
            excluded_by_id = {item.source_id: item for item in existing.excluded_sources if item.source_id}
            excluded = [excluded_by_id.get(item.source_id, item) for item in excluded]
        draft_values = {
            "workspace_path": str(path.resolve()),
            "metadata": content.metadata,
            "requirements": manifest.requirements,
            "template_id": template.id,
            "template_snapshot": template,
            "profile_id": profile.id,
            "profile_snapshot": profile,
            "parent_path": str(path.parent),
            "mode": manifest.mode,
            "sources": sources,
            "excluded_sources": excluded,
            "disk_inputs": self.disk_inputs(path),
        }
        if existing:
            draft_values["id"] = existing.id
        draft = WorkspaceDraft(**draft_values)
        self.library.save_draft(draft)
        self.settings["recent"] = [str(path.resolve()), *self.settings.get("recent", [])]
        self.settings["recent"] = list(dict.fromkeys(self.settings["recent"]))[:40]
        write_json(self.settings_path, self.settings)
        self._load_draft(draft)
        self.refresh_project_list(str(path.resolve()))

    def _render_buckets(self):
        for role, file_list in self.bucket_lists.items():
            file_list.clear()
            sources = [source for source in self.attachments.values() if source.role == role]
            if not sources:
                item = QListWidgetItem(self.bucket_empty_text[role])
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                file_list.addItem(item)
                file_list.setFixedHeight(56)
                continue
            file_list.setFixedHeight(min(150, 38 * len(sources) + 6))
            for source in sources:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, source.id)
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
                )
                card = FileCard(
                    source, self.open_attachment, self.edit_source_details, self.exclude_attachment
                )
                item.setSizeHint(card.sizeHint())
                file_list.addItem(item)
                file_list.setItemWidget(item, card)
        self.extract_title_button.setVisible(
            any(source.role == "requirements_style" for source in self.attachments.values())
        )

    def _render_excluded(self):
        self.excluded_list.clear()
        has_excluded = bool(self.excluded)
        self.excluded_toggle.setVisible(has_excluded)
        if not has_excluded:
            self.excluded_list.hide()
            return
        self.excluded_toggle.setText(f"Исключённые файлы · {len(self.excluded)}")
        for excluded in self.excluded.values():
            widget = QWidget()
            row = QHBoxLayout(widget)
            row.setContentsMargins(6, 2, 6, 2)
            row.addWidget(QLabel(excluded.name), 1)
            choose = button(
                "Найти / вернуть", lambda _checked=False, item_id=excluded.id: self.restore_excluded(item_id)
            )
            forget = QToolButton()
            forget.setText("×")
            forget.setToolTip("Удалить запись об исключённом файле")
            forget.clicked.connect(lambda _checked=False, item_id=excluded.id: self.forget_excluded(item_id))
            row.addWidget(choose)
            row.addWidget(forget)
            item = QListWidgetItem()
            item.setSizeHint(widget.sizeHint())
            self.excluded_list.addItem(item)
            self.excluded_list.setItemWidget(item, widget)
        self.excluded_list.setVisible(self.excluded_toggle.isChecked())

    def add_files_dialog(self, role: SourceRole, _checked=False):
        paths, _ = QFileDialog.getOpenFileNames(self, "Материалы", "", "Все файлы (*.*)")
        self.add_files(role, [Path(path) for path in paths])

    def add_files(
        self, role: SourceRole, paths: list[Path], *, title_metadata: bool = False, confidential: bool = False
    ):
        existing = {Path(source.path).resolve() for source in self.attachments.values() if source.path}
        for path in paths:
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                continue
            if not resolved.is_file() or resolved in existing:
                continue
            previously_excluded = next(
                (
                    item
                    for item in self.excluded.values()
                    if item.original_path and Path(item.original_path).resolve() == resolved
                ),
                None,
            )
            if previously_excluded:
                source = DraftSource(
                    source_id=previously_excluded.source_id,
                    name=previously_excluded.name,
                    path=str(resolved),
                    role=previously_excluded.role,
                    reference=previously_excluded.reference,
                    reference_verified=previously_excluded.reference_verified,
                    confidential=previously_excluded.confidential,
                    title_metadata=previously_excluded.title_metadata,
                )
                self.attachments[source.id] = source
                del self.excluded[previously_excluded.id]
                existing.add(resolved)
                continue
            source = DraftSource(
                path=str(resolved),
                name=resolved.name,
                role=role,
                title_metadata=title_metadata,
                confidential=confidential,
            )
            self.attachments[source.id] = source
            existing.add(resolved)
        self._render_buckets()
        self._refresh_title_sample_label()
        self.schedule_autosave()

    def move_attachment(self, source_id: str, role: SourceRole):
        source = self.attachments.get(source_id)
        if source is None or source.role == role:
            return
        source.role = role
        if role != "requirements_style":
            source.title_metadata = False
        self._refresh_title_sample_label()
        self._render_buckets()
        self.schedule_autosave()

    def attachment_path(self, source_id: str) -> Path | None:
        source = self.attachments.get(source_id)
        return Path(source.path) if source and source.path else None

    def open_attachment(self, source_id: str):
        path = self.attachment_path(source_id)
        if path:
            try:
                open_path(path)
            except ValueError as exc:
                self.show_error(str(exc))

    def edit_source_details(self, source_id: str):
        source = self.attachments.get(source_id)
        if source is None:
            return
        dialog = SourceDetailsDialog(source, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        source.reference = dialog.reference.text().strip()
        source.reference_verified = dialog.verified.isChecked()
        source.confidential = dialog.confidential.isChecked()
        if dialog.title_metadata.isChecked():
            for other in self.attachments.values():
                if other.id != source_id:
                    other.title_metadata = False
        source.title_metadata = dialog.title_metadata.isChecked()
        self._refresh_title_sample_label()
        self._render_buckets()
        self.schedule_autosave()

    def exclude_attachment(self, source_id: str):
        source = self.attachments.pop(source_id, None)
        if source is None:
            return
        path = Path(source.path)
        original_path = path
        if self.current_workspace and path.resolve().is_relative_to(self.current_workspace.resolve()):
            stash = self.library.directory / "excluded" / (self.draft.id if self.draft else "default")
            stash.mkdir(parents=True, exist_ok=True)
            original_path = stash / f"{source.id}-{safe_name(path.name)}"
            try:
                shutil.copy2(path, original_path)
            except OSError as exc:
                self.attachments[source_id] = source
                self.show_error(f"Не удалось сохранить локальную копию исключённого файла: {exc}")
                return
        excluded = DraftExcludedSource(
            source_id=source.source_id,
            name=source.name or path.name,
            original_path=str(original_path),
            confidential=source.confidential,
            role=source.role,
            reference=source.reference,
            reference_verified=source.reference_verified,
            title_metadata=source.title_metadata,
        )
        self.excluded[excluded.id] = excluded
        self._render_buckets()
        self._render_excluded()
        self._refresh_title_sample_label()
        self.schedule_autosave()

    def restore_excluded(self, excluded_id: str):
        excluded = self.excluded.get(excluded_id)
        if excluded is None:
            return
        path = Path(excluded.original_path) if excluded.original_path else None
        if path is None or not path.is_file():
            selected, _ = QFileDialog.getOpenFileName(self, "Найти исключённый файл", "", "Все файлы (*.*)")
            path = Path(selected) if selected else None
        if path is None:
            return
        source = DraftSource(
            source_id=excluded.source_id,
            name=excluded.name,
            path=str(path.resolve()),
            role=excluded.role,
            reference=excluded.reference,
            reference_verified=excluded.reference_verified,
            confidential=excluded.confidential,
            title_metadata=excluded.title_metadata,
        )
        self.attachments[source.id] = source
        del self.excluded[excluded_id]
        self._render_buckets()
        self._render_excluded()
        self._refresh_title_sample_label()
        self.schedule_autosave()

    def forget_excluded(self, excluded_id: str):
        excluded = self.excluded.pop(excluded_id, None)
        if excluded and excluded.original_path:
            path = Path(excluded.original_path)
            stash_root = (self.library.directory / "excluded").resolve()
            if path.is_relative_to(stash_root):
                path.unlink(missing_ok=True)
        self._render_excluded()
        self.schedule_autosave()

    def choose_title_sample(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Образец титульных данных",
            "",
            "Образцы (*.docx *.pdf *.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.txt *.md)",
        )
        if path:
            self.set_title_sample(Path(path))

    def extract_title_sample(self):
        candidates = [source for source in self.attachments.values() if source.role == "requirements_style"]
        if not candidates:
            self.choose_title_sample()
        elif len(candidates) == 1:
            self.set_title_sample(Path(candidates[0].path))
        else:
            dialog = QDialog(self)
            dialog.setWindowTitle("Выберите образец титула")
            layout = QVBoxLayout(dialog)
            selector = QComboBox()
            for source in candidates:
                selector.addItem(Path(source.path).name, source.path)
            layout.addWidget(selector)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.set_title_sample(Path(selector.currentData()))
            else:
                return
        if self.title_sample:
            sample = self.title_sample
            self.start_job(
                "Читаю явно подписанные реквизиты образца…",
                lambda: title_fields(analyze(sample, self.library.directory / "cache")["text"]),
                self.title_fields_extracted,
            )

    def title_fields_extracted(self, fields: dict[str, str]):
        count = 0
        for key, value in fields.items():
            if not self.metadata_fields[key].text().strip():
                self.metadata_fields[key].setText(value)
                count += 1
        self.schedule_autosave()
        self.status.setText(
            f"Заполнено полей: {count}. Существующие значения сохранены. Остальные реквизиты сверит внешний агент."
            if fields
            else "Явно подписанные реквизиты не найдены. Образец добавлен для сверки внешним агентом; OCR не выполнялся."
        )
        self.metadata_toggle.setChecked(True)
        self.toggle_metadata(True)

    def set_title_sample(self, path: Path | None):
        current_id = next((source.id for source in self.attachments.values() if source.title_metadata), None)
        if path is None:
            self.title_sample = None
            if current_id:
                self.exclude_attachment(current_id)
            self._render_title_sample_label()
            self._render_buckets()
            self.schedule_autosave()
            return

        if current_id:
            self.attachments[current_id].title_metadata = False
        resolved = path.resolve()
        existing = next(
            (
                source
                for source in self.attachments.values()
                if source.path and Path(source.path).resolve() == resolved
            ),
            None,
        )
        if existing:
            existing.role = "requirements_style"
            existing.title_metadata = True
            existing.confidential = True
        else:
            self.add_files("requirements_style", [resolved], title_metadata=True, confidential=True)
        self.title_sample = resolved
        self._render_title_sample_label()
        self._render_buckets()
        self.schedule_autosave()

    def _refresh_title_sample_label(self):
        source = next((item for item in self.attachments.values() if item.title_metadata), None)
        self.title_sample = Path(source.path) if source else None
        self._render_title_sample_label()

    def _render_title_sample_label(self):
        self.title_sample_label.setVisible(self.title_sample is not None)
        self.title_sample_label.setText(
            f"Титульные данные будут сверены по образцу: {self.title_sample.name}."
            if self.title_sample
            else "Образец титула не выбран."
        )
        self.title_sample_label.setToolTip(str(self.title_sample) if self.title_sample else "")
        self.clear_title_sample_button.setVisible(self.title_sample is not None)

    def toggle_metadata(self, expanded: bool):
        self._reflow_metadata()
        self.metadata_frame.setVisible(expanded)
        self._update_metadata_summary()

    def _reflow_metadata(self):
        columns = 1 if self.editor_scroll.viewport().width() < 600 else 2
        for index, cell in enumerate(self.metadata_cells):
            self.metadata_grid.addWidget(cell, index // columns, index % columns)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "metadata_cells"):
            QTimer.singleShot(0, self._reflow_metadata)

    def _update_metadata_summary(self):
        author = self.metadata_fields["author"].text().strip()
        group = self.metadata_fields["group"].text().strip()
        label = ", ".join(value for value in (author, group) if value) or "не заполнены"
        self.metadata_toggle.setText(
            f"Титульные данные · {label} · "
            + ("Свернуть" if self.metadata_toggle.isChecked() else "Развернуть / Изменить")
        )

    def toggle_requirements_focus(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Задание и требования")
        dialog.resize(820, 580)
        layout = QVBoxLayout(dialog)
        editor = QPlainTextEdit(self.requirements.toPlainText())
        editor.setPlaceholderText(self.requirements.placeholderText())
        layout.addWidget(editor)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.requirements.setPlainText(editor.toPlainText())

    def _selected_workspace_models(self):
        assert self.draft is not None
        self._capture_draft()
        template = self.selected_template()
        profile = self.selected_profile()
        manifest_sources = self.source_models()
        excluded_sources = [
            ExcludedSource(
                id=item.source_id or f"excluded-{index:03}",
                name=item.name,
                confidential=item.confidential,
            )
            for index, item in enumerate(self.excluded.values(), 1)
        ]
        workspace = Workspace(
            engine_version=__version__,
            name=self.draft.metadata.title,
            mode=self.draft.mode,
            sources=manifest_sources,
            excluded_sources=excluded_sources,
            requirements=self.draft.requirements,
            content_review_required=True,
            created_at=now(),
        )
        return workspace, template, profile

    def source_models(self) -> list[Source]:
        reserved = {
            source.source_id
            for source in [*self.attachments.values(), *self.excluded.values()]
            if source.source_id
        }
        index = 1
        result = []
        for source in self.attachments.values():
            source_id = source.source_id
            if not source_id:
                while f"s{index:03}" in reserved:
                    index += 1
                source_id = f"s{index:03}"
                reserved.add(source_id)
            name = source.name or Path(source.path).name
            role_path = SOURCE_ROLE_DIRECTORIES[source.role]
            result.append(
                Source(
                    id=source_id,
                    name=name,
                    path=f"{role_path}/{source_id}-{safe_name(name)}",
                    role=source.role,
                    reference=source.reference,
                    reference_verified=source.reference_verified,
                    confidential=source.confidential,
                    title_metadata=source.title_metadata,
                )
            )
        return result

    def _generated_instructions(self) -> str:
        if self.draft is None:
            return ""
        manifest, template, profile = self._selected_workspace_models()
        return generate_agents(manifest, template, profile, Path(self._display_path()))

    def _refresh_inspector(self):
        if not hasattr(self, "workspace_tree"):
            return
        previous = self.workspace_tree.currentItem()
        previous_virtual = previous.data(0, TREE_VIRTUAL_ROLE) if previous else ""
        previous_path = previous.data(0, Qt.ItemDataRole.UserRole) if previous else ""
        self.parent_input.setText(self._display_path())
        self.open_folder_button.setEnabled(bool(self.current_workspace and self.current_workspace.exists()))
        self.change_path_button.setEnabled(self.current_workspace is None)
        self.build_button.setEnabled(
            self.current_workspace is not None and self.draft is not None and not self.draft.pending_changes
        )
        self.workspace_tree.clear()
        root_path = Path(self._display_path())
        root = QTreeWidgetItem([root_path.name or str(root_path)])
        root.setData(0, Qt.ItemDataRole.UserRole, str(root_path) if root_path.exists() else "")
        self.workspace_tree.addTopLevelItem(root)
        files = [
            "INSTRUCTIONS.md",
            "task.md",
            "AGENTS.md",
            "workspace.json",
            "template.json",
            "profile.json",
            "document.json",
            "doc.cmd",
        ]
        if self.current_workspace:
            files.extend(path.name for path in self.current_workspace.glob("AGENTS.codexdoc-*.md"))
        for filename in files:
            actual = self.current_workspace / filename if self.current_workspace else None
            item = QTreeWidgetItem([filename])
            item.setData(0, Qt.ItemDataRole.UserRole, str(actual) if actual and actual.is_file() else "")
            item.setData(0, TREE_VIRTUAL_ROLE, f"virtual:{filename}")
            root.addChild(item)
        folder_items = {}
        for folder in ("sources", "context", "template", "norms", "assets", "analysis", "output"):
            item = QTreeWidgetItem([folder + "/"])
            actual_folder = self.current_workspace / folder if self.current_workspace else None
            item.setData(
                0,
                Qt.ItemDataRole.UserRole,
                str(actual_folder) if actual_folder and actual_folder.is_dir() else "",
            )
            root.addChild(item)
            folder_items[folder] = item
        if self.current_workspace and not (self.draft and self.draft.pending_changes):
            for folder in ("sources", "context", "template", "norms", "assets", "analysis", "output"):
                directory = self.current_workspace / folder
                if not directory.is_dir():
                    continue
                for path in sorted(directory.iterdir(), key=lambda value: value.name.casefold()):
                    if path.name.startswith("."):
                        continue
                    child = QTreeWidgetItem([path.name + ("/" if path.is_dir() else "")])
                    child.setData(0, Qt.ItemDataRole.UserRole, str(path))
                    folder_items[folder].addChild(child)
        for source, model in zip(self.attachments.values(), self.source_models(), strict=True):
            folder = SOURCE_ROLE_DIRECTORIES[source.role]
            filename = model.name
            expected = (
                self.current_workspace / folder / f"{model.id}-{safe_name(filename)}"
                if self.current_workspace
                else None
            )
            if expected and expected.is_file() and not (self.draft and self.draft.pending_changes):
                continue
            item = QTreeWidgetItem([f"{model.id}-{safe_name(filename)}"])
            original = Path(source.path)
            item.setData(0, Qt.ItemDataRole.UserRole, str(original) if original.is_file() else "")
            item.setData(0, TREE_VIRTUAL_ROLE, f"virtual:source:{source.id}")
            folder_items[folder].addChild(item)
        root.setExpanded(True)
        for item in folder_items.values():
            item.setExpanded(True)
        self.workspace_tree.setCurrentItem(
            self._find_tree_item(previous_virtual, previous_path) or root.child(0)
        )
        self.instructions_badge.setEnabled(self.instructions_path() is not None)
        self.instructions_badge.setText(
            self._active_instructions_name() + " ↗" if self.current_workspace else "INSTRUCTIONS.md ↗"
        )
        handoff_ready = self.instructions_path() is not None
        self.folder_badge.setEnabled(handoff_ready)
        self.workspace_tree.setDragEnabled(handoff_ready)
        self._refresh_status()

    def _find_tree_item(self, virtual_value, path_value):
        def visit(item):
            if (
                item.data(0, TREE_VIRTUAL_ROLE) == virtual_value
                and item.data(0, Qt.ItemDataRole.UserRole) == path_value
            ):
                return item
            for index in range(item.childCount()):
                found = visit(item.child(index))
                if found is not None:
                    return found
            return None

        for index in range(self.workspace_tree.topLevelItemCount()):
            found = visit(self.workspace_tree.topLevelItem(index))
            if found is not None:
                return found
        return None

    def preview_tree_item(self, item, _previous=None):
        try:
            self._preview_tree_item(item)
        except (ValueError, OSError) as exc:
            self._show_preview(str(exc), "Не удалось прочитать файл")

    def _preview_tree_item(self, item):
        if item is None:
            return
        value = item.data(0, TREE_VIRTUAL_ROLE) or item.data(0, Qt.ItemDataRole.UserRole)
        if value in {"virtual:AGENTS.md", "virtual:INSTRUCTIONS.md"}:
            self.preview_instructions()
            return
        if value == "virtual:task.md":
            self._show_preview(
                self.requirements.toPlainText() or "Задание пока не задано.", "Задание пользователя · task.md"
            )
            return
        if value == "virtual:workspace.json":
            try:
                manifest, _, _ = self._selected_workspace_models()
                self._show_preview(
                    json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2),
                    "Текущие правила workspace.json",
                )
            except (ValueError, OSError) as exc:
                self._show_preview(str(exc), "Предпросмотр недоступен")
            return
        if value == "virtual:template.json":
            self._show_preview(
                json.dumps(self.selected_template().model_dump(mode="json"), ensure_ascii=False, indent=2),
                "Текущий template.json",
            )
            return
        if value == "virtual:profile.json":
            self._show_preview(
                json.dumps(self.selected_profile().model_dump(mode="json"), ensure_ascii=False, indent=2),
                "Текущий profile.json",
            )
            return
        if value == "virtual:document.json":
            assert self.draft is not None
            content: dict = (
                read_json(self.current_workspace / "document.json")
                if self.current_workspace
                else {"blocks": []}
            )
            content["metadata"] = self.draft.metadata.model_dump(mode="json")
            self._show_preview(
                json.dumps(content, ensure_ascii=False, indent=2),
                "document.json · содержание и текущие реквизиты",
            )
            return
        if isinstance(value, str) and value.startswith("virtual:source:"):
            source_id = value.split(":", 2)[2]
            source = self.attachments.get(source_id)
            if source:
                path = Path(source.path)
                if (
                    path.is_file()
                    and path.suffix.lower() in TEXT_EXTENSIONS
                    and path.stat().st_size <= 600_000
                ):
                    self._show_preview(
                        analyze(path, self.library.directory / "cache")["text"],
                        f"{SOURCE_ROLE_LABELS[source.role]} · {source.name or path.name}",
                    )
                else:
                    self._show_preview(
                        f"{SOURCE_ROLE_LABELS[source.role]}\n\n{source.path}",
                        "Файл будет добавлен при подготовке папки",
                    )
            return
        if value:
            path = Path(value)
            if path.is_file() and path.suffix.lower() in {".md", ".txt", ".json", ".csv"}:
                try:
                    if path.stat().st_size > 600_000:
                        self._show_preview("Файл слишком велик для встроенного предпросмотра.", path.name)
                        return
                    text = path.read_text(encoding="utf-8-sig")
                    self._show_preview(text, f"Содержимое на диске · {path.name}")
                    return
                except (OSError, UnicodeError) as exc:
                    self._show_preview(str(exc), f"Не удалось прочитать · {path.name}")
                    return
        self._show_preview("Выберите текстовый файл, чтобы посмотреть его содержимое.", "Предпросмотр файла")

    def preview_instructions(self):
        try:
            generated = self._generated_instructions()
            self._show_preview(generated, "Живой предпросмотр INSTRUCTIONS.md")
        except (ValueError, OSError) as exc:
            self._show_preview(str(exc), "Предпросмотр инструкций недоступен")

    def _show_preview(self, text: str, caption: str):
        self.preview_caption.setText(caption)
        self.preview_text.setPlainText(text)

    def _refresh_report(self):
        report, stale = self.assess_build(self.current_workspace) if self.current_workspace else (None, "")
        pending = bool(self.draft and self.draft.pending_changes)
        stale = "Изменения черновика ещё не подготовлены" if pending else stale
        current = report is not None and not stale and bool(report.artifact_hashes)
        self.docx_button.setEnabled(
            bool(self.current_workspace and (self.current_workspace / "output/document.docx").is_file())
        )
        self.pdf_button.setEnabled(
            bool(self.current_workspace and (self.current_workspace / "output/document.pdf").is_file())
        )
        self.publish_button.setVisible(bool(self.draft and self.draft.mode == "template"))
        self.publish_button.setEnabled(current and report is not None and report.status == "ready")
        self.build_summary.setText(
            stale
            or (
                f"Последняя сборка · {report.page_count} стр."
                if current and report
                else "Сборка ещё не выполнялась"
            )
        )
        findings = report.findings if report else []
        groups = {
            "fields": ("Оглавление и поля", ("docx.toc", "toc.", "docx.page")),
            "context": ("Изоляция контекста", ("source.mention", "citation.forbidden")),
            "format": (
                "Библиография и титул",
                ("citation.unverified", "bibliography.", "title.", "docx.title"),
            ),
        }
        for key, (label, prefixes) in groups.items():
            failures = [f for f in findings if f.code.startswith(prefixes) and f.severity != "info"]
            text = "⚠ Требует проверки" if not current else "✓ Проверено"
            if failures:
                text = "❌ " + failures[0].message
            elif key == "context" and current:
                text = "✓ Запрещённых ссылок и имён файлов не найдено"
            self.validation_labels[key].setText(f"{label}: {text}")
            self.color_status(self.validation_labels[key])
        self.validation_labels["review"].setText(
            "Просмотр и содержание: ✓ Завершены"
            if current and report and report.status == "ready"
            else "Просмотр и содержание: требуется завершить по отчёту"
        )
        self.color_status(self.validation_labels["review"])
        self.color_status(self.build_summary)
        if not self.current_workspace:
            self.report_text.setPlainText(
                "Сначала подготовьте папку. После этого здесь появятся сборка DOCX/PDF и отчёт проверок."
            )
            return
        path = self.current_workspace / "output" / "checks.txt"
        if path.is_file():
            try:
                prefix = f"⚠ {stale}\nОтчёт ниже относится к предыдущему состоянию.\n\n" if stale else ""
                self.report_text.setPlainText(prefix + path.read_text(encoding="utf-8"))
            except OSError as exc:
                self.report_text.setPlainText(f"Не удалось прочитать отчёт: {exc}")
        else:
            self.report_text.setPlainText(
                "Папка подготовлена. Агент заполняет document.json; после этого соберите документ."
            )

    def copy_start_command(self):
        if (
            not self.current_workspace
            or (self.draft and self.draft.pending_changes)
            or not self._instructions_are_current(self.current_workspace)
        ):
            self.status.setText("Сначала подготовьте папку, чтобы команда соответствовала файлам на диске.")
            return
        path = self._display_path()
        instruction_name = self._active_instructions_name()
        text = (
            f"Изучи {instruction_name} и материалы в рабочей папке {path}, затем выполни задание из task.md (workspace.json → requirements) "
            "с соблюдением ролей источников и остальных ограничений."
        )
        QApplication.clipboard().setText(text)
        self.status.setText("Стартовая команда скопирована")

    def instructions_path(self) -> Path | None:
        if not self.current_workspace or (self.draft and self.draft.pending_changes):
            return None
        path = self.current_workspace / self._active_instructions_name()
        try:
            return (
                path
                if path.is_file() and path.read_text(encoding="utf-8") == self._generated_instructions()
                else None
            )
        except (OSError, ValueError):
            return None

    def _active_instructions_name(self) -> str:
        if not self.current_workspace:
            return "INSTRUCTIONS.md"
        try:
            generated = self._generated_instructions()
        except (ValueError, OSError):
            return "AGENTS.md"
        candidates = [
            self.current_workspace / "INSTRUCTIONS.md",
            self.current_workspace / "AGENTS.md",
            self.current_workspace / f"AGENTS.codexdoc-{__version__}.md",
            *sorted(self.current_workspace.glob(f"AGENTS.codexdoc-{__version__}-*.md")),
        ]
        for candidate in candidates:
            if candidate.is_file() and candidate.read_text(encoding="utf-8") == generated:
                return candidate.name
        return "AGENTS.md"

    def copy_instructions(self):
        try:
            QApplication.clipboard().setText(self._generated_instructions())
            self.status.setText("Текст инструкций скопирован")
        except (ValueError, OSError) as exc:
            self.show_error(str(exc))

    def choose_parent(self):
        if self.draft is None:
            return
        current = self.draft.parent_path if self.draft else str(WORKSPACES)
        path = QFileDialog.getExistingDirectory(self, "Папка для рабочих проектов", current)
        if path:
            self.draft.parent_path = path
            self.settings["workspace_parent"] = path
            write_json(self.settings_path, self.settings)
            self.parent_input.setText(self._display_path())
            self.schedule_autosave()

    def open_workspace_folder(self):
        path = (
            self.current_workspace
            if self.current_workspace
            else Path(self.draft.parent_path if self.draft else WORKSPACES)
        )
        try:
            open_path(path)
        except ValueError as exc:
            self.show_error(str(exc))

    def open_current(self, relative: str = ""):
        if self.current_workspace:
            try:
                open_path(self.current_workspace / relative)
            except ValueError as exc:
                self.show_error(str(exc))

    def save_workspace(self):
        if self.draft is None or self.worker is not None:
            return
        try:
            if (
                self.current_workspace
                and self.draft.disk_inputs
                and self.disk_inputs(self.current_workspace) != self.draft.disk_inputs
            ):
                dialog = QMessageBox(self)
                dialog.setWindowTitle("Проект изменён внешним агентом")
                dialog.setText(
                    "На диске изменены реквизиты, правила или настройки проекта. Подготовка заменит их значениями текущего черновика. Содержание разделов будет сохранено."
                )
                overwrite = dialog.addButton("Применить черновик", QMessageBox.ButtonRole.AcceptRole)
                reload_button = dialog.addButton("Загрузить с диска", QMessageBox.ButtonRole.DestructiveRole)
                dialog.addButton(QMessageBox.StandardButton.Cancel)
                dialog.exec()
                if dialog.clickedButton() == reload_button:
                    self._load_workspace(self.current_workspace)
                    return
                if dialog.clickedButton() != overwrite:
                    return
            self._capture_draft()
            self.library.save_draft(self.draft)
            template = self.selected_template()
            profile = self.selected_profile()
            metadata = self.draft.metadata.model_copy(deep=True)
            materials = [
                (Path(item.path), source)
                for item, source in zip(self.attachments.values(), self.source_models(), strict=True)
            ]
            excluded_sources = [
                ExcludedSource(
                    id=item.source_id or f"excluded-{index:03}",
                    name=item.name,
                    confidential=item.confidential,
                )
                for index, item in enumerate(self.excluded.values(), 1)
            ]
            mode = self.draft.mode
            workspace_path = Path(self.draft.workspace_path) if self.draft.workspace_path else None
            requirements = self.draft.requirements
            parent_path = Path(
                self.draft.parent_path or self.settings.get("workspace_parent", str(WORKSPACES))
            )
            self.settings["workspace_parent"] = str(parent_path)
            self.settings["template"] = template.id
            self.settings["profile"] = profile.id
            write_json(self.settings_path, self.settings)

            def prepare():
                if workspace_path and workspace_path.exists():
                    return update_workspace(
                        workspace_path,
                        metadata,
                        template,
                        profile,
                        materials,
                        requirements,
                        mode,
                        self.library,
                        excluded_sources,
                    )
                return create_workspace(
                    parent_path,
                    metadata,
                    template,
                    profile,
                    materials,
                    requirements,
                    mode,
                    self.library,
                    excluded_sources,
                )

            self.start_job("Подготавливаю рабочую папку…", prepare, self.workspace_prepared)
        except (ValueError, OSError) as exc:
            self.show_error(str(exc))

    def workspace_prepared(self, path: Path):
        assert self.draft is not None
        manifest, template, profile, _ = load_workspace(path)
        self.draft.workspace_path = str(path.resolve())
        self.draft.template_snapshot = template
        self.draft.profile_snapshot = profile
        self.draft.pending_changes = False
        for attachment, source in zip(self.attachments.values(), manifest.sources, strict=True):
            attachment.source_id = source.id
            attachment.path = str((path / source.path).resolve())
            attachment.name = source.name
        self._capture_draft()
        self.draft.disk_inputs = self.disk_inputs(path)
        self.current_workspace = path
        self.settings["recent"] = [str(path.resolve()), *self.settings.get("recent", [])]
        self.settings["recent"] = list(dict.fromkeys(self.settings["recent"]))[:40]
        write_json(self.settings_path, self.settings)
        self.library.save_draft(self.draft)
        self._render_buckets()
        self._render_excluded()
        self._refresh_inspector()
        self._refresh_status()
        self._refresh_report()
        self.refresh_project_list(str(path.resolve()))
        self.status.setText("Рабочая папка подготовлена. Запустите внешнего агента отдельно.")
        self._observed_stamp = self.workspace_stamp()

    @staticmethod
    def disk_inputs(path: Path) -> dict:
        manifest, template, profile, content = load_workspace(path)
        return {
            "manifest": manifest.model_dump(),
            "template": template.model_dump(),
            "profile": profile.model_dump(),
            "metadata": content.metadata.model_dump(),
        }

    def start_job(self, message, function, callback):
        if self.worker is not None:
            return
        self.prepare_button.setEnabled(False)
        self.build_button.setEnabled(False)
        self._autosave_timer.stop()
        self.sidebar.setEnabled(False)
        self.editor_scroll.setEnabled(False)
        self.inspector_tabs.setEnabled(False)
        self.change_path_button.setEnabled(False)
        self._job_started_at = time.monotonic()
        self.progress.show()
        self.status.setText(message)
        self.result_callback = callback
        self.worker = Worker(function)
        self.worker.signals.success.connect(self.job_success)
        self.worker.signals.failure.connect(self.job_failure)
        self.pool.start(self.worker)

    def job_success(self, value):
        callback = self.result_callback
        self.worker = None
        self.result_callback = None
        self.progress.hide()
        self.sidebar.setEnabled(True)
        self.editor_scroll.setEnabled(True)
        self.inspector_tabs.setEnabled(True)
        self.change_path_button.setEnabled(self.current_workspace is None)
        self.prepare_button.setEnabled(True)
        self.build_button.setEnabled(
            self.current_workspace is not None and self.draft is not None and not self.draft.pending_changes
        )
        if callback:
            callback(value)

    def job_failure(self, message: str):
        self.worker = None
        self.result_callback = None
        self.progress.hide()
        self.sidebar.setEnabled(True)
        self.editor_scroll.setEnabled(True)
        self.inspector_tabs.setEnabled(True)
        self.change_path_button.setEnabled(self.current_workspace is None)
        self.prepare_button.setEnabled(True)
        self.build_button.setEnabled(
            self.current_workspace is not None and self.draft is not None and not self.draft.pending_changes
        )
        self.show_error(message)

    def show_error(self, message: str):
        self.status.setText("Операция не завершена; черновик сохранён.")
        QMessageBox.warning(self, "Не удалось выполнить действие", message)

    def run_pipeline(self, action: str):
        if self.worker is not None:
            return
        if self.current_workspace is None:
            self.status.setText("Сначала подготовьте рабочую папку.")
            return
        if self.draft and self.draft.pending_changes:
            self.status.setText("Сначала подготовьте несохранённые изменения в рабочей папке.")
            return
        self.persist_current_draft()
        self.inspector_tabs.setCurrentIndex(1)
        path = self.current_workspace
        function = build if action == "build" else check
        message = (
            "Собираю DOCX/PDF и проверяю результат…" if action == "build" else "Проверяю текущую сборку…"
        )
        self.start_job(message, lambda: function(path), self.pipeline_finished)

    def pipeline_finished(self, report: CheckReport):
        self._refresh_report()
        self._refresh_status()
        self.refresh_project_list(str(self.current_workspace.resolve()) if self.current_workspace else None)
        self.status.setText(
            {
                "ready": "Сборка актуальна, проверки и просмотр пройдены; содержательная экспертиза остаётся за пользователем.",
                "review": "Документ собран. Завершите оставшуюся проверку по отчёту.",
                "preview": "Предпросмотр собран; это не итоговая проверка.",
                "blocked": "Проверка нашла проблемы; подробности в отчёте.",
            }[report.status]
        )
        self.status.setText(
            f"Операция завершена за {time.monotonic() - self._job_started_at:.1f} с. {self.status.text()}"
        )
        self._observed_stamp = self.workspace_stamp()

    def show_review(self):
        if self.current_workspace is None:
            self.status.setText("Сначала подготовьте и соберите документ.")
            return
        if not (self.current_workspace / "output" / "checks.json").is_file():
            self.show_error("Сначала соберите документ.")
            return
        try:
            report = check(self.current_workspace)
            if not report.page_count:
                self.show_error("Нет актуальных страниц для просмотра. Соберите документ.")
                return
            ReviewDialog(self.current_workspace, self).exec()
            self._refresh_report()
            self._refresh_status()
        except (ValueError, OSError) as exc:
            self.show_error(str(exc))

    def edit_author_profile(self):
        dialog = AuthorProfileDialog(self.library.author_profile(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.library.save_author_profile(dialog.value())
            self.settings = read_json(self.settings_path)
            self.status.setText("Профиль автора сохранён для новых документов.")

    def edit_agent_profile(self):
        if self.draft is None:
            return
        profile = self.selected_profile()
        dialog = ProfileDialog(profile, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            value = dialog.value()
            self.library.save_profile(value)
            self.profiles = self.library.profiles()
            self.draft.profile_id = value.id
            self.draft.profile_snapshot = value
            self.refresh_library()
            self.profile_combo.setCurrentIndex(self.profile_combo.findData(value.id))
            self.schedule_autosave()

    def show_template_library(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Библиотека шаблонов")
        dialog.resize(650, 430)
        layout = QVBoxLayout(dialog)
        self.template_list = QListWidget()
        for template in self.library.templates():
            item = QListWidgetItem(template.name + (" · черновик" if template.status == "draft" else ""))
            item.setData(Qt.ItemDataRole.UserRole, template.id)
            item.setToolTip("\n".join([template.agent_notes, *template.unresolved]).strip())
            self.template_list.addItem(item)
        layout.addWidget(self.template_list, 1)
        row = QGridLayout()
        for index, (label, callback) in enumerate(
            [
                ("Выбрать", self.use_selected_template),
                ("Настроить копию…", self.edit_template),
                ("Создать по образцам…", self.start_template),
                ("Импортировать…", self.import_template),
                ("Экспортировать…", self.export_template),
                ("Профили агента…", self.edit_agent_profile),
            ]
        ):
            row.addWidget(button(label, callback), index // 3, index % 3)
        layout.addLayout(row)
        layout.addWidget(
            muted(
                "Шаблон задаёт оформление; применимость норм и методических правил проверяется по материалам задания."
            )
        )
        dialog.exec()
        self.template_list = None
        self._capture_draft()
        self.refresh_library()

    def use_selected_template(self):
        if not self.template_list or not self.template_list.currentItem():
            return
        template_id = self.template_list.currentItem().data(Qt.ItemDataRole.UserRole)
        self.template_combo.setCurrentIndex(self.template_combo.findData(template_id))
        self.schedule_autosave()

    def edit_template(self):
        try:
            template = (
                self.library.get_template(self.template_list.currentItem().data(Qt.ItemDataRole.UserRole))
                if self.template_list and self.template_list.currentItem()
                else self.selected_template()
            )
            dialog = TemplateDialog(template, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                value = dialog.value()
                self.library.save_template(value)
                self.refresh_library()
                self.template_combo.setCurrentIndex(self.template_combo.findData(value.id))
                self.status.setText("Шаблон сохранён в библиотеке.")
        except (ValueError, OSError) as exc:
            self.show_error(str(exc))

    def start_template(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Методички и образцы оформления", "", "Все файлы (*.*)")
        if not paths:
            return
        self.new_document()
        assert self.draft is not None
        self.draft.mode = "template"
        self.title_input.setText("Анализ нового шаблона")
        self.add_files("requirements_style", [Path(path) for path in paths])
        self.status.setText("Создан проект анализа образцов. Содержание и промпт задаёт пользователь.")

    def cancel_template_mode(self):
        if self.draft:
            self.draft.mode = "document"
            self.schedule_autosave()

    def import_template(self):
        path, _ = QFileDialog.getOpenFileName(self, "Импорт шаблона", "", "Шаблон CodexDoc (*.json)")
        if not path:
            return
        try:
            template = Template.model_validate(read_json(Path(path)))
            template.id = "import-" + __import__("uuid").uuid4().hex[:10]
            self.library.save_template(template)
            self.refresh_library()
        except (ValueError, OSError) as exc:
            self.show_error(str(exc))

    def export_template(self):
        try:
            template = (
                self.library.get_template(self.template_list.currentItem().data(Qt.ItemDataRole.UserRole))
                if self.template_list and self.template_list.currentItem()
                else self.selected_template()
            )
            path, _ = QFileDialog.getSaveFileName(
                self, "Экспорт шаблона", template.id + ".json", "Шаблон CodexDoc (*.json)"
            )
            if path:
                write_json(Path(path), template)
                self.status.setText("Настройки шаблона экспортированы.")
        except (ValueError, OSError) as exc:
            self.show_error(str(exc))

    def publish_current(self):
        if self.current_workspace is None:
            return
        path = self.current_workspace
        self.start_job(
            "Проверяю и сохраняю шаблон…",
            lambda: publish_template(path, self.library),
            self.template_published,
        )

    def template_published(self, path):
        self.refresh_library()
        self.status.setText("Проверенный шаблон сохранён в библиотеке: " + path.name)

    def closeEvent(self, event):
        if self.worker is not None:
            self.status.setText("Дождитесь завершения текущей операции, затем закройте приложение.")
            event.ignore()
        elif not self.persist_current_draft():
            event.ignore()
        else:
            self._watch_timer.stop()
            self._autosave_timer.stop()
            self.theme.dispose()
            event.accept()


__all__ = ["MainWindow", "STUDIO_STYLE"]

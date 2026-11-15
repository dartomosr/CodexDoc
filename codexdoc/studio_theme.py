"""Official Rosé Pine Main / Dawn mapped to shared semantic UI tokens."""

from string import Template

from PySide6.QtGui import QColor, QPalette

ROSE_LIGHT = {
    "bg_canvas": "#faf4ed",
    "bg_surface": "#fffaf3",
    "bg_subtle": "#f2e9e1",
    "bg_hover": "#f4ede8",
    "bg_active_item": "#dfdad9",
    "border_default": "#dfdad9",
    "border_hover": "#cecacd",
    "border_focus": "#907aa9",
    "text_primary": "#575279",
    "text_secondary": "#797593",
    "text_muted": "#9893a5",
    "accent_primary": "#d7827e",
    "accent_hover": "#b4637a",
    "accent_text": "#fffaf3",
    "accent_tint": "#f4ede8",
    "accent_foreground": "#b4637a",
    "bucket_citable": "#56949f",
    "bucket_context": "#ea9d34",
    "bucket_style": "#907aa9",
    "success": "#286983",
    "warning": "#ea9d34",
    "error": "#b4637a",
    "scroll_thumb": "rgba(152, 147, 165, 105)",
}
ROSE_DARK = {
    "bg_canvas": "#191724",
    "bg_surface": "#1f1d2e",
    "bg_subtle": "#26233a",
    "bg_hover": "#21202e",
    "bg_active_item": "#403d52",
    "border_default": "#403d52",
    "border_hover": "#524f67",
    "border_focus": "#c4a7e7",
    "text_primary": "#e0def4",
    "text_secondary": "#908caa",
    "text_muted": "#6e6a86",
    "accent_primary": "#ebbcba",
    "accent_hover": "#eb6f92",
    "accent_text": "#191724",
    "accent_tint": "#21202e",
    "accent_foreground": "#eb6f92",
    "bucket_citable": "#9ccfd8",
    "bucket_context": "#f6c177",
    "bucket_style": "#c4a7e7",
    "success": "#31748f",
    "warning": "#f6c177",
    "error": "#eb6f92",
    "scroll_thumb": "rgba(110, 106, 134, 105)",
}


def tokens_for(dark: bool = False) -> dict[str, str]:
    return dict(ROSE_DARK if dark else ROSE_LIGHT)


def rose_palette(tokens: dict[str, str]) -> QPalette:
    palette = QPalette()
    roles = {
        "Window": "bg_canvas",
        "WindowText": "text_primary",
        "Base": "bg_subtle",
        "AlternateBase": "bg_surface",
        "Text": "text_primary",
        "Button": "bg_surface",
        "ButtonText": "text_primary",
        "BrightText": "accent_text",
        "Highlight": "accent_primary",
        "HighlightedText": "accent_text",
        "ToolTipBase": "bg_surface",
        "ToolTipText": "text_primary",
        "PlaceholderText": "text_muted",
        "Light": "bg_hover",
        "Midlight": "bg_surface",
        "Mid": "border_default",
        "Dark": "bg_canvas",
        "Shadow": "border_default",
        "Link": "accent_foreground",
        "LinkVisited": "bucket_style",
        "Accent": "accent_primary",
    }
    for role, token in roles.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(tokens[token]))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(tokens["text_muted"]))
    return palette


def status_tone(text: str) -> str:
    if "❌" in text or "Ошибка" in text:
        return "error"
    if "⚠" in text or "Требует" in text or "требуется" in text or "не обновлена" in text:
        return "warning"
    if "✓" in text:
        return "success"
    return "text_secondary"


def studio_style(dark: bool = False) -> str:
    return Template(_QSS).substitute(tokens_for(dark))


_QSS = """
QWidget { font-family: 'Segoe UI'; font-size: 10pt; color: $text_primary; background: $bg_surface; }
QMainWindow, QDialog, QWidget#studioRoot, QWidget#sidebar, QWidget#inspector, QFrame#sidebarFooter { background: $bg_canvas; }
QWidget#editorPage { background: $bg_surface; }
QWidget#sidebar { border-radius: 8px; }
QFrame#surface, QFrame#bucketCard, QFrame#inspectorSurface, QFrame#card {
    background: $bg_surface; border: 1px solid $border_default; border-radius: 8px;
}
QFrame#bucketCard[bucketRole="citable"] { border-left: 3px solid $bucket_citable; }
QFrame#bucketCard[bucketRole="context_only"] { border-left: 3px solid $bucket_context; }
QFrame#bucketCard[bucketRole="requirements_style"] { border-left: 3px solid $bucket_style; }
QLabel { background: transparent; }
QLabel#brand { font-size: 18pt; font-weight: 600; }
QLabel#title { font-size: 22pt; font-weight: 600; }
QLabel#sectionTitle { font-size: 10pt; font-weight: 600; }
QLabel#eyebrow { color: $text_secondary; font-size: 8pt; font-weight: 600; }
QLabel#muted { color: $text_secondary; font-size: 9pt; }
QLabel#fileType { color: $text_secondary; font-size: 8pt; font-weight: 600; }
QLabel#statusBadge { background: $accent_tint; color: $accent_foreground; border-radius: 5px; padding: 4px 8px; font-size: 8pt; }
QLabel[tone="success"], QLabel#muted[tone="success"], QLabel#statusBadge[tone="success"] { color: $success; }
QLabel[tone="warning"], QLabel#muted[tone="warning"], QLabel#statusBadge[tone="warning"] { color: $warning; }
QLabel[tone="error"], QLabel#muted[tone="error"], QLabel#statusBadge[tone="error"] { color: $error; }
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    color: $text_primary; background: $bg_subtle; border: 1px solid $border_default;
    border-radius: 6px; padding: 6px 8px; selection-background-color: $accent_primary;
    selection-color: $accent_text;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { min-height: 20px; }
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: $border_focus; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { color: $text_muted; }
QLineEdit#documentTitle { font-size: 15pt; font-weight: 600; padding: 8px; }
QComboBox QAbstractItemView { color: $text_primary; background: $bg_subtle; selection-background-color: $bg_active_item; selection-color: $text_primary; border: 1px solid $border_default; outline: 0; }
QMenu, QToolTip { background: $bg_surface; color: $text_primary; border: 1px solid $border_default; padding: 6px; }
QMenu::item { padding: 6px 18px; }
QMenu::item:selected { background: $bg_hover; color: $accent_foreground; }
QMenu::item:disabled { color: $text_muted; }
QMenu::separator { background: $border_default; height: 1px; margin: 4px 8px; }
QPushButton, QToolButton {
    color: $text_primary; background: $bg_surface; border: 1px solid $border_default;
    border-radius: 6px; padding: 6px 10px;
}
QPushButton:hover, QToolButton:hover { background: $bg_hover; border-color: $border_hover; }
QPushButton:focus, QToolButton:focus { border-color: $border_focus; }
QPushButton:disabled, QToolButton:disabled { color: $text_muted; background: $bg_subtle; border-color: $border_default; }
QPushButton#primary { background: $accent_primary; color: $accent_text; border-color: $accent_primary; font-weight: 600; }
QPushButton#primary:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#primary:disabled { background: $bg_subtle; color: $text_muted; border-color: $border_default; }
QPushButton#metadataToggle { text-align: left; background: $bg_surface; }
QToolButton#fileAction { padding: 0; border: 0; background: transparent; }
QToolButton#fileAction:hover { background: $bg_hover; color: $accent_foreground; }
QPlainTextEdit#previewText, QLabel#workspacePath, QTreeWidget { font-family: 'Cascadia Code', Consolas, monospace; font-size: 9pt; }
QPlainTextEdit#previewText { background: $bg_subtle; }
QListWidget, QTableWidget, QTreeWidget { background: $bg_subtle; color: $text_primary; border: 1px solid $border_default; border-radius: 6px; selection-background-color: $bg_active_item; selection-color: $text_primary; gridline-color: $border_default; }
QListWidget#projectList { border: 0; background: transparent; outline: 0; }
QListWidget#projectList::item { border-radius: 6px; padding: 8px; margin: 2px 0; border: 1px solid transparent; border-left: 3px solid transparent; }
QListWidget#projectList::item:selected { background: $bg_hover; border: 1px solid $border_default; border-left: 3px solid $accent_primary; }
QListWidget#projectList::item:hover { background: $bg_hover; }
QListWidget#bucketList { background: $bg_subtle; border: 1px dashed $border_default; outline: 0; }
QListWidget#bucketList::item { padding: 0; color: $text_secondary; }
QListWidget#bucketList::item:selected { background: $bg_active_item; }
QTreeWidget::item { min-height: 23px; }
QTreeWidget::item:selected { background: $bg_active_item; color: $text_primary; }
QHeaderView::section { background: $bg_subtle; color: $text_secondary; border: 0; border-bottom: 1px solid $border_default; padding: 6px; }
QTabWidget::pane { background: $bg_surface; border: 1px solid $border_default; border-radius: 6px; }
QTabBar::tab { background: $bg_canvas; color: $text_secondary; padding: 8px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { background: $bg_surface; color: $accent_foreground; border-bottom-color: $accent_foreground; }
QTabBar::tab:hover { background: $bg_hover; }
QScrollArea { border: none; background: transparent; }
QSplitter::handle { background: $border_default; }
QSplitter::handle:hover { background: $accent_tint; }
QProgressBar { border: none; background: $bg_subtle; max-height: 3px; }
QProgressBar::chunk { background: $accent_primary; }
QCheckBox, QRadioButton { spacing: 6px; background: transparent; }
QGroupBox { border: 1px solid $border_default; border-radius: 8px; margin-top: 10px; padding-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; color: $text_secondary; }
QScrollBar:vertical { width: 8px; background: transparent; margin: 0; }
QScrollBar:horizontal { height: 8px; background: transparent; margin: 0; }
QScrollBar::handle { background: $scroll_thumb; border-radius: 4px; min-height: 24px; min-width: 24px; }
QScrollBar::handle:hover { background: $text_secondary; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; background: transparent; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
"""

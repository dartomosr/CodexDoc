"""Follow Windows appearance without changing OS settings or overriding Qt's OS hint."""

import ctypes
import logging
import sys
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

from codexdoc.studio_theme import rose_palette, studio_style, tokens_for

THEME_MODES = ("auto", "light", "dark")


def registry_dark() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        ) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def system_dark() -> bool:
    scheme = QGuiApplication.styleHints().colorScheme()
    return registry_dark() if scheme == Qt.ColorScheme.Unknown else scheme == Qt.ColorScheme.Dark


def set_title_bar(window: QWidget, dark: bool) -> bool:
    if sys.platform != "win32" or not window.isWindow():
        return False
    try:
        api = ctypes.WinDLL("dwmapi")
        setter = api.DwmSetWindowAttribute
        setter.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        setter.restype = ctypes.c_long
        enabled = wintypes.BOOL(dark)
        handle = wintypes.HWND(int(window.winId()))
        for attribute in (20, 19):
            if setter(handle, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)) >= 0:
                return True
    except (OSError, AttributeError):
        logging.debug("DWM appearance is unavailable", exc_info=True)
    return False


class WindowsThemeEvents(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        if sys.platform == "win32":
            native = wintypes.MSG.from_address(int(message))
            if native.message in (0x001A, 0x031A):  # WM_SETTINGCHANGE / WM_THEMECHANGED
                self.callback()
        return False, 0


class ThemeController(QObject):
    changed = Signal()

    def __init__(self, application: QApplication, mode: str = "auto", parent=None):
        super().__init__(parent)
        self.application = application
        self.mode = mode if mode in THEME_MODES else "auto"
        self.system_is_dark = system_dark()
        self.dark = False
        self.tokens = tokens_for()
        self._framing = False
        self._disposed = False
        self._system_timer = QTimer(self)
        self._system_timer.setSingleShot(True)
        self._system_timer.timeout.connect(self.refresh_system)
        self.native_events = WindowsThemeEvents(lambda: self._system_timer.start(0))
        application.installNativeEventFilter(self.native_events)
        application.installEventFilter(self)
        QGuiApplication.styleHints().colorSchemeChanged.connect(self.system_scheme_changed)
        # Fusion uses the supplied palette for every control, including spin
        # buttons and popup arrows, instead of keeping Windows light assets.
        application.setStyle("Fusion")
        application.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
        self.apply()

    def set_mode(self, mode: str):
        if mode not in THEME_MODES:
            raise ValueError(f"Неизвестная тема: {mode}")
        self.mode = mode
        if mode == "auto":
            self.system_is_dark = system_dark()
        self.apply()

    def system_scheme_changed(self, scheme):
        self.system_is_dark = (
            registry_dark() if scheme == Qt.ColorScheme.Unknown else scheme == Qt.ColorScheme.Dark
        )
        if self.mode == "auto":
            self.apply()

    def refresh_system(self):
        self.system_is_dark = system_dark()
        if self.mode == "auto":
            self.apply()

    def apply(self):
        self.dark = self.system_is_dark if self.mode == "auto" else self.mode == "dark"
        self.tokens = tokens_for(self.dark)
        self.application.setPalette(rose_palette(self.tokens))
        self.application.setStyleSheet(studio_style(self.dark))
        for window in self.application.topLevelWidgets():
            if window.isVisible():
                self.frame(window)
        self.changed.emit()

    def frame(self, window):
        if self._framing:
            return
        self._framing = True
        try:
            set_title_bar(window, self.dark)
        finally:
            self._framing = False

    def eventFilter(self, watched, event):
        if (
            isinstance(watched, QWidget)
            and watched.isWindow()
            and event.type() in (QEvent.Type.Show, QEvent.Type.WinIdChange)
        ):
            if watched.isVisible():
                self.frame(watched)
        return False

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self._system_timer.stop()
        self.application.removeEventFilter(self)
        self.application.removeNativeEventFilter(self.native_events)
        QGuiApplication.styleHints().colorSchemeChanged.disconnect(self.system_scheme_changed)

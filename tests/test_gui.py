import time

import pytest
from PySide6.QtCore import QByteArray, QMimeData, QPointF, Qt
from PySide6.QtGui import QDropEvent, QGuiApplication, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog

from codexdoc.gui import STYLE, MainWindow, ProfileDialog, TemplateDialog
from codexdoc.models import AgentProfile, AuthorProfile
from codexdoc.storage import ROOT, read_json, write_json
from codexdoc.studio import SOURCE_MIME_TYPE
from codexdoc.studio_theme import ROSE_DARK, ROSE_LIGHT
from codexdoc.workspaces import load_workspace


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setStyleSheet(STYLE)
    yield instance


def wait_for_worker(window, app, timeout=20):
    deadline = time.monotonic() + timeout
    while window.worker is not None and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(30)
    assert window.worker is None


def test_gui_prepare_workspace_through_button(app, library, tmp_path):
    window = MainWindow(library)
    window.show()
    window.draft.parent_path = str(tmp_path / "projects")
    window.settings["workspace_parent"] = str(tmp_path / "projects")
    window.title_input.setText("Задание из интерфейса")
    window.metadata_fields["author"].setText("Иванов И.И.")
    window.requirements.setPlainText("Включить цель и выводы.")
    material = tmp_path / "материал.txt"
    material.write_text("Описание выполненной работы", encoding="utf-8")
    window.add_files("context_only", [material])
    QTest.mouseClick(window.save_button, Qt.MouseButton.LeftButton)
    wait_for_worker(window, app)
    assert window.current_workspace is not None
    manifest = read_json(window.current_workspace / "workspace.json")
    assert manifest["sources"][0]["role"] == "context_only"
    assert "policy" not in manifest["sources"][0]
    assert manifest["requirements"] == "Включить цель и выводы."
    assert read_json(window.current_workspace / "document.json")["metadata"]["author"] == "Иванов И.И."
    assert (window.current_workspace / "AGENTS.md").is_file()
    assert window.project_status.text() == "Готов к агенту"
    assert window.build_button.isEnabled()
    window.close()


def test_gui_profile_and_template_forms(app, library):
    dialog = ProfileDialog(AgentProfile())
    dialog.internet.setCurrentIndex(0)
    assert dialog.value().internet == "off"
    template = TemplateDialog(library.get_template("lab"))
    template.fields["left_mm"].setValue(25)
    assert template.value().page.left_mm == 25
    assert template.value().id != "lab"
    template.close()
    dialog.close()


def test_author_profile_prefills_new_documents(app, library):
    library.save_author_profile(
        AuthorProfile(
            organization="Университет",
            department="Кафедра",
            author="Иванов И.И.",
            group="КТ-01",
            city="Таганрог",
        )
    )
    window = MainWindow(library)
    assert window.metadata_fields["organization"].text() == "Университет"
    assert window.metadata_fields["author"].text() == "Иванов И.И."
    assert window.metadata_fields["group"].text() == "КТ-01"
    assert window.metadata_fields["city"].text() == "Таганрог"
    window.close()


def test_title_sample_is_prepared_for_agent(app, library, tmp_path, monkeypatch):
    sample = tmp_path / "Титульный образец.txt"
    sample.write_text("Автор: Иванов И.И.\nГруппа: КТ-01\nГород: Таганрог", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(sample), ""))
    window = MainWindow(library)
    window.show()
    window.choose_title_sample()
    assert window.title_sample == sample
    assert sample.name in window.title_sample_label.text()
    assert window.metadata_fields["group"].text() == ""
    window.metadata_fields["author"].setText("Данные пользователя")
    window.title_input.setText("Текущая работа")
    window.draft.parent_path = str(tmp_path / "projects")
    window.settings["workspace_parent"] = str(tmp_path / "projects")
    QTest.mouseClick(window.save_button, Qt.MouseButton.LeftButton)
    wait_for_worker(window, app)
    assert window.current_workspace is not None
    workspace = window.current_workspace
    source = read_json(workspace / "workspace.json")["sources"][0]
    assert source["title_metadata"] is True
    assert source["role"] == "requirements_style"
    assert source["confidential"] is True
    assert (workspace / source["path"]).read_text(encoding="utf-8") == sample.read_text(encoding="utf-8")
    metadata = read_json(workspace / "document.json")["metadata"]
    assert metadata["author"] == "Данные пользователя"
    assert metadata["title"] == "Текущая работа"
    assert metadata["group"] == ""
    instructions = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Титульные данные из образца" in instructions
    assert source["path"] in instructions
    assert "Сохраняй непустые значения" in instructions
    assert "не заменяй" in instructions
    window.close()


def test_title_sample_cancel_and_exclude(app, library, tmp_path, monkeypatch):
    window = MainWindow(library)
    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"temporary reference")
    window.set_title_sample(sample)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("", ""))
    window.choose_title_sample()
    assert window.title_sample == sample
    window.set_title_sample(None)
    assert window.title_sample is None
    assert "не выбран" in window.title_sample_label.text().lower()
    assert window.clear_title_sample_button.isHidden()
    assert len(window.excluded) == 1
    window.close()


@pytest.fixture
def studio(app, library, tmp_path):
    write_json(library.directory / "settings.json", {"workspace_parent": str(tmp_path / "projects")})
    window = MainWindow(library)
    yield window
    wait_for_worker(window, app)
    window.close()


def test_draft_survives_switch_and_restart(studio, app, library):
    first_id = studio.draft.id
    studio.title_input.setText("Первая работа")
    studio.requirements.setPlainText("Сохранить даже до срабатывания таймера")
    studio.new_document()
    studio.title_input.setText("Вторая работа")
    item = next(
        studio.project_list.item(i)
        for i in range(studio.project_list.count())
        if studio.project_list.item(i).data(Qt.ItemDataRole.UserRole)["draft_id"] == first_id
    )
    studio.project_list.setCurrentItem(item)
    assert studio.title_input.text() == "Первая работа"
    assert "до срабатывания" in studio.requirements.toPlainText()
    studio.close()
    restored = MainWindow(library)
    assert restored.draft.id == first_id
    assert restored.requirements.toPlainText() == studio.requirements.toPlainText()
    assert len(library.drafts()) == 2
    restored.close()


def test_prepare_twice_preserves_names_blocks_and_handoff(studio, app, tmp_path):
    material = tmp_path / "контекст.txt"
    material.write_text("Внутренние сведения", encoding="utf-8")
    studio.title_input.setText("Моя работа")
    studio.requirements.setPlainText("Точное пользовательское задание")
    studio.add_files("context_only", [material])
    studio.save_workspace()
    wait_for_worker(studio, app)
    workspace = studio.current_workspace
    manifest, _, _, content = load_workspace(workspace)
    original_path = manifest.sources[0].path
    content.blocks = [{"type": "paragraph", "id": "agent-text", "text": "Содержание агента"}]
    write_json(workspace / "document.json", content)
    studio.requirements.setPlainText("Обновлённое задание")
    studio.save_workspace()
    wait_for_worker(studio, app)
    manifest, _, _, content = load_workspace(workspace)
    assert manifest.sources[0].name == material.name
    assert manifest.sources[0].path == original_path
    assert content.blocks[0].id == "agent-text"
    assert "Обновлённое задание" in (workspace / "task.md").read_text(encoding="utf-8")
    assert (workspace / "INSTRUCTIONS.md").read_text(encoding="utf-8") == studio._generated_instructions()
    assert studio.instructions_path() == workspace / "INSTRUCTIONS.md"
    studio.copy_start_command()
    assert "INSTRUCTIONS.md" in QApplication.clipboard().text()
    assert str(workspace) in QApplication.clipboard().text()
    studio.preview_tree_item(
        studio._find_tree_item("virtual:document.json", str(workspace / "document.json"))
    )
    assert "Содержание агента" in studio.preview_text.toPlainText()


def test_clean_project_refreshes_external_metadata(studio, app):
    studio.save_workspace()
    wait_for_worker(studio, app)
    _, _, _, content = load_workspace(studio.current_workspace)
    content.metadata.author = "Автор из образца, заполненный агентом"
    write_json(studio.current_workspace / "document.json", content)
    studio.refresh_external_changes()
    assert studio.metadata_fields["author"].text() == content.metadata.author
    assert not studio.draft.pending_changes


def test_bucket_drop_moves_once_and_clears_title_role(studio, tmp_path):
    material = tmp_path / "sample.txt"
    material.write_text("Группа: КТ-01", encoding="utf-8")
    studio.set_title_sample(material)
    source_id = next(iter(studio.attachments))
    mime = QMimeData()
    mime.setData(SOURCE_MIME_TYPE, QByteArray(source_id.encode()))
    event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.MoveAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    studio.bucket_lists["context_only"].dropEvent(event)
    assert event.isAccepted()
    assert len(studio.attachments) == 1
    assert studio.attachments[source_id].role == "context_only"
    assert studio.attachments[source_id].title_metadata is False
    assert studio.title_sample is None
    assert studio.bucket_lists["context_only"].count() == 1


def test_local_title_extraction_preserves_existing_fields(studio, app, tmp_path):
    sample = tmp_path / "title.txt"
    sample.write_text("Автор: Автор из образца\nГруппа: КТ-02\nГород: Таганрог", encoding="utf-8")
    studio.title_input.setText("Тема текущей работы")
    studio.metadata_fields["author"].setText("Мой автор")
    studio.add_files("requirements_style", [sample])
    studio.extract_title_sample()
    assert not studio.sidebar.isEnabled()
    draft_id = studio.draft.id
    studio.new_document()
    assert studio.draft.id == draft_id
    wait_for_worker(studio, app)
    assert studio.metadata_fields["group"].text() == "КТ-02"
    assert studio.metadata_fields["city"].text() == "Таганрог"
    assert studio.metadata_fields["author"].text() == "Мой автор"
    assert studio.title_input.text() == "Тема текущей работы"


@pytest.mark.parametrize("size", [(1100, 720), (1920, 1080)])
def test_studio_layout_fits_window(studio, app, size):
    studio.resize(*size)
    studio.show()
    app.processEvents()
    assert studio.width() == size[0]
    assert studio.editor_scroll.horizontalScrollBar().maximum() == 0
    assert studio.editor_page.minimumSizeHint().width() <= studio.editor_scroll.viewport().width()
    for control in (studio.prepare_button, studio.build_button, studio.inspector_tabs):
        assert control.isVisible()
        assert control.mapTo(studio, control.rect().bottomRight()).y() < studio.height()
    studio.metadata_toggle.click()
    studio.people_fields["authors"].add_person()
    app.processEvents()
    assert studio.editor_scroll.horizontalScrollBar().maximum() == 0


def test_preview_ids_stay_unique_after_removal(studio, app, tmp_path):
    paths = [tmp_path / f"{i}.txt" for i in range(3)]
    for path in paths:
        path.write_text("Материал", encoding="utf-8")
    studio.add_files("citable", paths[:2])
    studio.save_workspace()
    wait_for_worker(studio, app)
    studio.exclude_attachment(next(iter(studio.attachments)))
    studio.add_files("context_only", paths[2:])
    models = studio.source_models()
    assert len({source.id for source in models}) == len(models)
    assert models[1].id not in {item.source_id for item in studio.excluded.values()}


@pytest.mark.integration
@pytest.mark.skipif(
    not (ROOT / ".runtime/libreoffice/runtime.json").exists(), reason="Managed renderer is not installed"
)
def test_gui_build_and_stale_report(studio, app, workspace, monkeypatch):
    errors = []
    monkeypatch.setattr(studio, "show_error", errors.append)
    studio._load_workspace(workspace)
    studio.run_pipeline("build")
    assert studio.inspector_tabs.currentIndex() == 1
    wait_for_worker(studio, app, timeout=180)
    assert not errors
    assert studio.docx_button.isEnabled()
    assert studio.pdf_button.isEnabled()
    assert "✓" in studio.validation_labels["context"].text()
    _, _, _, content = load_workspace(workspace)
    content.metadata.title = "Изменение после сборки"
    write_json(workspace / "document.json", content)
    studio.refresh_external_changes()
    assert "пересборк" in studio.build_summary.text()
    assert "✓" not in studio.validation_labels["fields"].text()
    assert "предыдущему состоянию" in studio.report_text.toPlainText()


@pytest.mark.parametrize("size", [(1100, 720), (1920, 1080)])
def test_rose_modes_follow_system_and_preserve_workspace(studio, app, tmp_path, size, monkeypatch):
    import codexdoc.theme_controller as themes

    frames = []
    monkeypatch.setattr(themes, "set_title_bar", lambda window, dark: frames.append((window, dark)))
    studio.save_workspace()
    wait_for_worker(studio, app)
    workspace = studio.current_workspace
    before = {str(p.relative_to(workspace)): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    studio.resize(*size)
    studio.show()
    dialog = QDialog(studio)
    dialog.show()
    app.processEvents()
    try:
        for mode, tokens in (("light", ROSE_LIGHT), ("dark", ROSE_DARK)):
            studio.theme_combo.setCurrentIndex(studio.theme_combo.findData(mode))
            app.processEvents()
            assert studio.theme.mode == mode
            assert studio.theme.tokens == tokens
            assert app.palette().color(QPalette.ColorRole.Window).name() == tokens["bg_canvas"].lower()
            assert (
                app.palette().color(QPalette.ColorRole.PlaceholderText).name()
                == tokens["text_muted"].lower()
            )
            assert (dialog, mode == "dark") in frames
            assert (studio, mode == "dark") in frames
            assert read_json(studio.settings_path)["theme_mode"] == mode
            assert studio.editor_scroll.horizontalScrollBar().maximum() == 0
            assert studio.project_status.geometry().right() < studio.editor_page.width()
            # An OS event must not override an explicit user choice.
            QGuiApplication.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Light)
            assert studio.theme.dark == (mode == "dark")
        studio.theme_combo.setCurrentIndex(studio.theme_combo.findData("auto"))
        for scheme in (Qt.ColorScheme.Dark, Qt.ColorScheme.Light):
            QGuiApplication.styleHints().colorSchemeChanged.emit(scheme)
            assert studio.theme.dark == (scheme == Qt.ColorScheme.Dark)
        after = {str(p.relative_to(workspace)): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
        assert after == before
        assert not studio.draft.pending_changes
    finally:
        dialog.close()


def test_rose_mode_survives_restart(studio, app, library):
    studio.theme_combo.setCurrentIndex(studio.theme_combo.findData("dark"))
    studio.close()
    restored = MainWindow(library)
    try:
        assert restored.theme.mode == "dark"
        assert restored.theme.dark
        assert restored.theme_combo.currentData() == "dark"
    finally:
        restored.close()


def test_unknown_qt_scheme_uses_registry(monkeypatch):
    import codexdoc.theme_controller as themes

    class Hints:
        def colorScheme(self):
            return Qt.ColorScheme.Unknown

    monkeypatch.setattr(themes.QGuiApplication, "styleHints", lambda: Hints())
    monkeypatch.setattr(themes, "registry_dark", lambda: True)
    assert themes.system_dark()
    monkeypatch.setattr(themes, "registry_dark", lambda: False)
    assert not themes.system_dark()

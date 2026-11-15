"""Templates and profiles are immutable snapshots inside each workspace."""

from pathlib import Path

from codexdoc import __version__
from codexdoc.models import AgentProfile, AuthorProfile, Provenance, Template, WorkspaceDraft
from codexdoc.storage import DATA, ROOT, read_json, write_json


def builtin_templates() -> list[Template]:
    standard = Provenance(
        title="ГОСТ 7.32-2017",
        version="2017; текст с поправками в локальной нормативной базе",
        location="norms/gost-7.32-2017.pdf",
        scope="Базовое оформление по 6.1, 6.2, 6.3, 6.4, 6.5, 6.6. Не сертификация полного соответствия отчёта о НИР.",
    )
    base = Template(
        id="report",
        name="Отчёт · ГОСТ 7.32, базовое оформление",
        engine_version=__version__,
        provenance=[standard],
        agent_notes="Применимость ГОСТ к заданию и требования к титулу уточняй по методичке. 14 пт выбран пресетом; ГОСТ требует не менее 12 пт. Структурные заголовки ВВЕДЕНИЕ, ЗАКЛЮЧЕНИЕ не нумеруются. Полное соответствие стандарту требует содержательного нормоконтроля.",
    )
    result = [base]
    for template_id, name, sections, toc in [
        (
            "lab",
            "Лабораторная работа",
            ["Цель работы", "Теоретические сведения", "Ход работы", "Результаты", "Выводы"],
            False,
        ),
        ("essay", "Реферат", ["Введение", "Основная часть", "Заключение"], True),
        (
            "course",
            "Курсовая работа",
            ["Введение", "Аналитическая часть", "Проектная часть", "Заключение"],
            True,
        ),
        (
            "explanatory",
            "Пояснительная записка",
            ["Введение", "Требования", "Проектные решения", "Проверка результатов", "Заключение"],
            True,
        ),
    ]:
        result.append(
            base.model_copy(
                update={"id": template_id, "name": name, "suggested_sections": sections, "toc": toc}
            )
        )
    return result


class Library:
    def __init__(self, directory: Path = DATA):
        self.directory = directory

    def templates(self) -> list[Template]:
        result = {t.id: t for t in builtin_templates()}
        for path in sorted((self.directory / "templates").glob("*.json")):
            item = Template.model_validate(read_json(path))
            result[item.id] = self._current_template(item)
        return list(result.values())

    @staticmethod
    def _current_template(template: Template) -> Template:
        if template.engine_version == __version__:
            return template
        previous = template.engine_version or "неизвестной версии"
        upgraded = template.model_copy(deep=True)
        upgraded.engine_version = __version__
        upgraded.status = "draft"
        note = (
            f"Повторно проверьте унаследованный шаблон после обновления движка с {previous} до {__version__}."
        )
        if note not in upgraded.unresolved:
            upgraded.unresolved.append(note)
        return upgraded

    def get_template(self, template_id: str) -> Template:
        for template in self.templates():
            if template.id == template_id:
                return template
        raise ValueError(f"Шаблон не найден: {template_id}")

    def save_template(self, template: Template) -> Path:
        template = self._current_template(template)
        path = self.directory / "templates" / f"{template.id}.json"
        if path.exists():
            old = Template.model_validate(read_json(path))
            template = template.model_copy(update={"revision": old.revision + 1})
            write_json(self.directory / "history" / template.id / f"{old.revision}.json", old)
        write_json(path, template)
        return path

    def profiles(self) -> list[AgentProfile]:
        result = {
            "balanced": AgentProfile(),
            "careful": AgentProfile(
                id="careful",
                name="Тщательная проверка",
                autonomy="guided",
                verification="strict",
                internet="off",
            ),
            "autonomous": AgentProfile(
                id="autonomous", name="Самостоятельный", autonomy="autonomous", internet="allowed"
            ),
        }
        for path in sorted((self.directory / "profiles").glob("*.json")):
            profile = AgentProfile.model_validate(read_json(path))
            result[profile.id] = profile
        return list(result.values())

    def save_profile(self, profile: AgentProfile):
        write_json(self.directory / "profiles" / f"{profile.id}.json", profile)

    def author_profile(self) -> AuthorProfile:
        path = self.directory / "settings.json"
        settings = read_json(path) if path.exists() else {}
        saved = settings.get("author_profile")
        if isinstance(saved, dict):
            return AuthorProfile.model_validate(saved)
        legacy = settings.get("metadata", {})
        fields = AuthorProfile.model_fields
        return AuthorProfile.model_validate({key: legacy[key] for key in fields if key in legacy})

    def save_author_profile(self, profile: AuthorProfile):
        path = self.directory / "settings.json"
        settings = read_json(path) if path.exists() else {}
        settings["author_profile"] = profile.model_dump(mode="json")
        write_json(path, settings)

    def drafts(self) -> list[WorkspaceDraft]:
        directory = self.directory / "drafts"
        if not directory.exists():
            return []
        return [WorkspaceDraft.model_validate(read_json(path)) for path in sorted(directory.glob("*.json"))]

    def save_draft(self, draft: WorkspaceDraft):
        write_json(self.directory / "drafts" / f"{draft.id}.json", draft)

    def delete_draft(self, draft_id: str):
        (self.directory / "drafts" / f"{draft_id}.json").unlink(missing_ok=True)

    def norm_catalog(self):
        path = ROOT / "norms" / "catalog.json"
        return read_json(path) if path.exists() else []

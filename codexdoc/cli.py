"""One documented command surface for people, generated launchers and agents."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from codexdoc import __version__
from codexdoc.analysis import analyze
from codexdoc.content_review import initialize, prepare, submit
from codexdoc.content_review import status as content_status
from codexdoc.extensions import run_extension
from codexdoc.library import Library
from codexdoc.models import AgentProfile, Metadata, Template
from codexdoc.pipeline import build, check, fingerprint, patch_block, publish_template, review
from codexdoc.previews import render_source_pages
from codexdoc.runtime import font_inventory, renderer_info
from codexdoc.storage import WORKSPACES, read_json, write_json
from codexdoc.workspaces import (
    create_workspace,
    example_content,
    refresh_instructions,
    source_settings,
    workspace_inventory,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="codexdoc", description="Локальная подготовка и проверка DOCX/PDF")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("gui", help="Открыть приложение")
    commands.add_parser("templates", help="Показать библиотеку")
    semantic = commands.add_parser("content-review", help="Постановка, пакет и заключение агента")
    semantic.add_argument("workspace", type=Path)
    semantic.add_argument("action", choices=["init", "prepare", "submit", "status"])
    semantic.add_argument("--stage", choices=["plan", "requirements", "final"], default="final")
    semantic.add_argument("--input", type=Path)
    doctor = commands.add_parser("doctor", help="Проверить окружение")
    doctor.add_argument("workspace", nargs="?", type=Path)
    create = commands.add_parser("create", help="Подготовить рабочую папку")
    create.add_argument("--name", default="Новый документ")
    create.add_argument("--template", default="report")
    create.add_argument("--parent", type=Path, default=WORKSPACES)
    create.add_argument("--source", type=Path, action="append", default=[])
    create.add_argument("--template-analysis", action="store_true")
    create.add_argument(
        "--example", action="store_true", help="Заполнить техническим проверочным содержанием"
    )
    inspect = commands.add_parser("inspect", help="Состояние рабочей папки или анализ файла")
    inspect.add_argument("workspace", type=Path)
    inspect.add_argument("--output", type=Path)
    refresh = commands.add_parser(
        "refresh-instructions", help="Сгенерировать текущие инструкции рядом с устаревшими"
    )
    refresh.add_argument("workspace", type=Path)
    source_preview = commands.add_parser("source-preview", help="Локально отрендерить страницы PDF-источника")
    source_preview.add_argument("workspace", type=Path)
    source_preview.add_argument("--id", required=True, help="ID источника из workspace.json")
    source_preview.add_argument("--page", type=int, action="append", dest="pages")
    extension = commands.add_parser("extension", help="Запустить зарегистрированный модуль")
    extension.add_argument("workspace", type=Path)
    extension.add_argument("--name", required=True)
    extension.add_argument("--request", type=Path)
    for name in ("build", "check", "review", "patch", "publish-template"):
        command = commands.add_parser(name)
        command.add_argument("workspace", type=Path)
        if name == "build":
            command.add_argument("--force", action="store_true")
            command.add_argument(
                "--preview", action="store_true", help="Собрать черновик без статуса готовности"
            )
        if name == "review":
            command.add_argument("--page", type=int, required=True)
            command.add_argument("--note", required=True)
        if name == "patch":
            command.add_argument("--id", required=True)
            command.add_argument("--text", required=True)
    return result


def main(arguments: list[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    try:
        if args.command == "gui":
            from codexdoc.gui import run

            return run()
        if args.command == "doctor":
            print(
                json.dumps(
                    {
                        "python": sys.version.split()[0],
                        "renderer": renderer_info(),
                        "fonts": font_inventory("Times New Roman"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "templates":
            print(json.dumps([t.model_dump() for t in Library().templates()], ensure_ascii=False, indent=2))
            return 0
        if args.command == "content-review":
            if args.action == "init":
                value = str(initialize(args.workspace))
            elif args.action == "prepare":
                value = str(prepare(args.workspace, args.stage))
            elif args.action == "submit":
                if args.input is None:
                    raise ValueError("Укажите --input с заключением агента")
                value = submit(args.workspace, args.input)
            else:
                value = content_status(args.workspace)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        if args.command == "create":
            path = create_workspace(
                args.parent,
                Metadata(title=args.name),
                Library().get_template(args.template),
                AgentProfile(),
                [
                    (p, source_settings(p, "format" if args.template_analysis else "cite"))
                    for p in args.source
                ],
                mode="template" if args.template_analysis else "document",
            )
            if args.example:
                write_json(path / "document.json", example_content())
            print(path)
            return 0
        if args.command == "inspect":
            if args.workspace.is_file():
                value = analyze(args.workspace)
            else:
                workspace_data = read_json(args.workspace / "workspace.json")
                template_data = read_json(args.workspace / "template.json")
                prepared_with = workspace_data.get("engine_version", "unknown")
                outdated = prepared_with != __version__
                template_engine = template_data.get("engine_version", "")
                template_outdated = template_engine != __version__
                current_instructions = args.workspace / f"AGENTS.codexdoc-{__version__}.md"
                inventory = workspace_inventory(args.workspace)
                inventory_issues = any(
                    inventory[key]
                    for key in ("missing", "invalid_provenance", "untracked", "hash_mismatches")
                )
                check_path = args.workspace / "output" / "checks.json"
                previous_checks = read_json(check_path) if check_path.exists() else "Ещё не собран"
                semantic = content_status(args.workspace)
                checks_current = False
                if isinstance(previous_checks, dict) and not (
                    outdated or template_outdated or inventory_issues
                ):
                    try:
                        key, _ = fingerprint(args.workspace, Template.model_validate(template_data))
                        checks_current = (
                            previous_checks.get("fingerprint") == key
                            and previous_checks.get("content_review") == semantic
                        )
                    except (ValueError, OSError, RuntimeError):
                        checks_current = False
                value = {
                    "workspace": workspace_data,
                    "template": {
                        "id": template_data.get("id", ""),
                        "name": template_data.get("name", ""),
                        "status": template_data.get("status", "draft"),
                        "engine_version": template_engine or "unknown",
                        "needs_revalidation": template_outdated,
                    },
                    "engine": {
                        "prepared_with": prepared_with,
                        "current": __version__,
                        "instructions_may_be_outdated": outdated,
                        "current_instructions": (
                            current_instructions.relative_to(args.workspace).as_posix()
                            if current_instructions.is_file()
                            else ""
                        ),
                        "notice": (
                            (
                                f"Рабочая папка создана старой версией. Откройте {current_instructions.name} и обновите проверки."
                                if current_instructions.is_file()
                                else "Рабочая папка создана старой версией. Выполните refresh-instructions, чтобы создать новые инструкции без перезаписи оригинала."
                            )
                            if outdated
                            else "Рабочая папка создана текущей версией движка."
                        ),
                    },
                    "inventory": inventory,
                    "content_review": semantic,
                    "checks": previous_checks,
                    "checks_current": checks_current,
                    "checks_notice": (
                        "Сохранённый статус может не учитывать текущие файлы, шаблон или правила движка. Повторно проверьте шаблон и запустите check/build перед использованием статуса и перед публикацией."
                        if not checks_current
                        else "Сохранённая проверка относится к текущей версии движка и чистому inventory."
                    ),
                }
            if args.output:
                write_json(args.output, value)
            else:
                print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        if args.command == "source-preview":
            pages = render_source_pages(args.workspace, args.id, args.pages)
            print(json.dumps([str(path) for path in pages], ensure_ascii=False, indent=2))
            return 0
        if args.command == "refresh-instructions":
            print(refresh_instructions(args.workspace))
            return 0
        if args.command == "extension":
            result = run_extension(
                args.workspace, args.name, read_json(args.request) if args.request else None
            )
            print(result.model_dump_json(indent=2))
            return 2 if any(f.severity == "error" for f in result.findings) else 0
        if args.command == "patch":
            patch_block(args.workspace, args.id, args.text)
            print("Блок изменён. Выполните build.")
            return 0
        if args.command == "publish-template":
            print(publish_template(args.workspace))
            return 0
        if args.command == "build":
            report = build(args.workspace, force=args.force, preview=args.preview)
        elif args.command == "check":
            report = check(args.workspace)
        else:
            report = review(args.workspace, args.page, args.note)
        print(json.dumps(report.model_dump(), ensure_ascii=False, indent=2))
        # Code 3 is actionable review pending, not an engine crash.
        return {"ready": 0, "review": 3, "preview": 0, "blocked": 2}[report.status]
    except (ValueError, ValidationError, OSError, RuntimeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

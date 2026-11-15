"""Local review packets and versioned agent attestations; no automatic AI calls."""

from pathlib import Path

from codexdoc import __version__
from codexdoc.models import ContentAssessment, ContentBaseline, Workspace
from codexdoc.storage import contained, digest, now, object_digest, read_json, workspace_lock, write_json
from codexdoc.workspaces import load_workspace

CRITERIA = {
    "plan": ["task_coverage", "scope", "source_sufficiency", "uncertainties"],
    "requirements": ["feature_preservation", "atomicity", "acceptance", "unsupported_decisions"],
    "final": [
        "task_coverage",
        "scope",
        "feature_preservation",
        "source_support",
        "argumentation",
        "consistency",
        "acceptance",
        "editorial_quality",
    ],
}


def initialize(workspace: Path) -> Path:
    """Opt existing workspaces in without rewriting documents or existing baseline."""
    with workspace_lock(workspace):
        manifest, _, _, _ = load_workspace(workspace)
        manifest.content_review_required = True
        write_json(workspace / "workspace.json", manifest)
        path = workspace / "analysis" / "baseline.json"
        if not path.exists():
            write_json(path, {"goal": "", "scope": "", "items": []})
        write_json(workspace / "schemas" / "baseline.schema.json", ContentBaseline.model_json_schema())
        from codexdoc.models import DocumentContent

        write_json(workspace / "schemas" / "document.schema.json", DocumentContent.model_json_schema())
        write_json(workspace / "schemas" / "workspace.schema.json", Workspace.model_json_schema())
        write_json(
            workspace / "schemas" / "content-review.schema.json", ContentAssessment.model_json_schema()
        )
        return path


def snapshot(workspace: Path) -> dict:
    manifest, template, profile, content = load_workspace(workspace)
    baseline = ContentBaseline.model_validate(read_json(workspace / "analysis" / "baseline.json"))
    sources = {s.id: s for s in manifest.sources}
    for item in baseline.items:
        if item.basis.kind == "source":
            source = sources.get(item.basis.source_id)
            if source is None or source.role not in {"citable", "context_only"}:
                raise ValueError(f"Недопустимое основание содержания: {item.id}")
        elif item.basis.source_id:
            raise ValueError(f"source_id допустим только для основания source: {item.id}")
    files = {}
    for source in manifest.sources:
        if source.path:
            files[source.path] = digest(contained(workspace, source.path))
    for block in content.blocks:
        if block.type == "image":
            files[block.path] = digest(contained(workspace, block.path))
    for evidence in template.provenance:
        if evidence.location:
            files[evidence.location] = digest(contained(workspace, evidence.location))
    data = {
        "engine": __version__,
        "manifest": manifest.model_dump(),
        "template": template.model_dump(),
        "profile": profile.model_dump(),
        "document": content.model_dump(),
        "baseline": baseline.model_dump(),
        "files": files,
    }
    return {"fingerprint": object_digest(data), **data}


def prepare(workspace: Path, stage: str) -> Path:
    if stage not in CRITERIA:
        raise ValueError("Неизвестный этап проверки")
    with workspace_lock(workspace):
        data = snapshot(workspace)
        if stage != "plan":
            previous = "plan" if stage == "requirements" else "requirements"
            if status(workspace)["stages"][previous]["status"] != "passed":
                raise ValueError(
                    f"Сначала завершите этап {previous}; изменения потребуют повторной проверки."
                )
        path = workspace / "analysis" / f"review-packet-{stage}.json"
        write_json(
            path,
            {
                **data,
                "stage": stage,
                "criteria_required": CRITERIA[stage],
                "instructions": "Внутренний пакет. Прочитайте разрешённые источники. Сопоставьте каждое положение с блоками; различайте требования пользователя, контекст, допущения и предложения. Не считайте прошлый отчёт утверждённой спецификацией. Для каждого критерия запишите вывод с конкретными основаниями. Замечания: код, важность, фрагмент/id блока, причина и исправление в message. Context не цитировать и не упоминать в итоговом документе. Не подтверждайте непроверенное.",
                "response_example": {
                    "fingerprint": data["fingerprint"],
                    "stage": stage,
                    "reviewer": "",
                    "summary": "",
                    "criteria": dict.fromkeys(CRITERIA[stage], ""),
                    "coverage": [
                        {"item_id": item["id"], "status": "unresolved", "block_ids": [], "note": ""}
                        for item in data["baseline"]["items"]
                    ],
                    "findings": [],
                },
            },
        )
        return path


def validate_assessment(workspace: Path, assessment: ContentAssessment, data: dict):
    if (
        not assessment.reviewer.strip()
        or not assessment.summary.strip()
        or any(not c.note.strip() for c in assessment.coverage)
    ):
        raise ValueError("Проверяющий, вывод и пояснения покрытия не могут быть пустыми")
    if assessment.fingerprint != data["fingerprint"]:
        raise ValueError("Заключение устарело: изменились текст, постановка, источники или настройки")
    required = CRITERIA[assessment.stage]
    if set(assessment.criteria) != set(required) or any(not v.strip() for v in assessment.criteria.values()):
        raise ValueError("Нужно дать обоснованный вывод по каждому критерию этапа")
    items = {item["id"]: item for item in data["baseline"]["items"]}
    covered = [entry.item_id for entry in assessment.coverage]
    if len(covered) != len(set(covered)) or set(covered) != set(items):
        raise ValueError("Карта покрытия должна включать каждое положение ровно один раз")
    blocks = {b["id"] for b in data["document"]["blocks"]}
    for entry in assessment.coverage:
        if set(entry.block_ids) - blocks:
            raise ValueError(f"Неизвестные блоки: {entry.item_id}")
        if entry.status == "covered" and not entry.block_ids:
            raise ValueError(f"Укажите блоки, раскрывающие {entry.item_id}")
    for finding in assessment.findings:
        if not finding.message.strip() or (finding.block_id and finding.block_id not in blocks):
            raise ValueError("Замечание пусто или ссылается на неизвестный блок")


def assessment_passed(assessment: ContentAssessment) -> bool:
    return all(entry.status == "covered" for entry in assessment.coverage) and not any(
        f.severity == "error" for f in assessment.findings
    )


def submit(workspace: Path, input_path: Path) -> dict:
    with workspace_lock(workspace):
        assessment = ContentAssessment.model_validate(read_json(input_path))
        data = snapshot(workspace)
        packet = read_json(workspace / "analysis" / f"review-packet-{assessment.stage}.json")
        if packet["fingerprint"] != data["fingerprint"]:
            raise ValueError("Пакет устарел; выполните content-review prepare заново")
        validate_assessment(workspace, assessment, data)
        if assessment.stage != "plan":
            previous = "plan" if assessment.stage == "requirements" else "requirements"
            if status(workspace)["stages"][previous]["status"] != "passed":
                raise ValueError(f"Не завершён этап {previous}")
        result = {"assessment": assessment.model_dump(), "submitted_at": now()}
        history = (
            workspace / "analysis" / "review-history" / f"{assessment.stage}-{object_digest(result)}.json"
        )
        write_json(history, result)
        write_json(workspace / "analysis" / f"review-result-{assessment.stage}.json", result)
        return status(workspace)


def status(workspace: Path) -> dict:
    manifest = Workspace.model_validate(read_json(workspace / "workspace.json"))
    result = {"required": manifest.content_review_required, "status": "not_checked", "stages": {}}
    if not manifest.content_review_required:
        result["status"] = "not_required"
        return result
    try:
        data = snapshot(workspace)
        result["fingerprint"] = data["fingerprint"]
        for stage in CRITERIA:
            path = workspace / "analysis" / f"review-result-{stage}.json"
            value = {"status": "pending"}
            if path.exists():
                assessment = ContentAssessment.model_validate(read_json(path)["assessment"])
                try:
                    validate_assessment(workspace, assessment, data)
                    if assessment.stage != stage:
                        raise ValueError("Этап заключения не соответствует файлу")
                    value = {
                        "status": "passed" if assessment_passed(assessment) else "issues",
                        "assessment": assessment.model_dump(),
                    }
                except ValueError:
                    value = {"status": "stale"}
            result["stages"][stage] = value
        states = [v["status"] for v in result["stages"].values()]
        result["status"] = (
            "passed"
            if all(s == "passed" for s in states)
            else "issues"
            if "issues" in states
            else "stale"
            if "stale" in states
            else "pending"
        )
    except (ValueError, OSError) as exc:
        result["status"] = "invalid_baseline"
        result["message"] = str(exc)
    return result

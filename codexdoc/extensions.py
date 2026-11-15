"""Trusted in-process entry points and subprocess adapters with one result contract.

Adapters isolate dependencies, not security permissions. Installing an adapter is
an explicit local configuration action, never an operation driven by source text.
"""

import importlib.metadata
import json
import os
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import Field

from codexdoc.models import Finding, Model
from codexdoc.storage import DATA, contained, digest, object_digest, read_json, write_json


class ExtensionResult(Model):
    artifacts: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    metrics: dict[str, int | float | str] = Field(default_factory=dict)


class Adapter(Model):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    version: str
    kind: Literal["process"] = "process"
    command: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=60, ge=1, le=600)


def word_count(request: dict) -> ExtensionResult:
    document = request["document"]
    strings = [b.get("text", "") for b in document["blocks"]]
    return ExtensionResult(
        metrics={"words": sum(len(t.split()) for t in strings), "characters": sum(len(t) for t in strings)}
    )


def run_extension(
    workspace: Path, name: str, payload: dict | None = None, config: Path = DATA / "extensions.json"
) -> ExtensionResult:
    if not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Недопустимое имя расширения.")
    request = {
        "workspace": str(workspace.resolve()),
        "payload": payload or {},
        "document": read_json(workspace / "document.json"),
        "manifest": read_json(workspace / "workspace.json"),
    }
    # Only explicitly registered providers are eligible. No source-folder discovery.
    provider = word_count if name == "word-count" else None
    adapter = None
    identity = "builtin-v1"
    if provider is None:
        entries = list(importlib.metadata.entry_points(group="codexdoc.extensions", name=name))
        if entries:
            provider = entries[0].load()
            identity = entries[0].value + ":" + (entries[0].dist.version if entries[0].dist else "unknown")
        else:
            adapters = [Adapter.model_validate(item) for item in read_json(config)] if config.exists() else []
            adapter = next((item for item in adapters if item.name == name), None)
            if adapter is None:
                raise ValueError(f"Расширение не зарегистрировано: {name}")
            identity = adapter.model_dump()
    dependencies = {
        str(p.relative_to(workspace)): digest(p)
        for folder in ("sources", "assets", "norms")
        for p in sorted((workspace / folder).rglob("*"))
        if p.is_file()
    }
    key = object_digest({"request": request, "provider": identity, "dependencies": dependencies})
    cache = workspace / ".cache" / "extensions" / name / f"{key}.json"
    if cache.exists():
        cached = read_json(cache)
        if all(
            contained(workspace, p).is_file() and digest(contained(workspace, p)) == sha
            for p, sha in cached["hashes"].items()
        ):
            return ExtensionResult.model_validate(cached["result"])
    if provider:
        raw = provider(request)
        result = raw if isinstance(raw, ExtensionResult) else ExtensionResult.model_validate(raw)
    else:
        assert adapter is not None
        child_env = None
        if Path(adapter.command[0]).stem.casefold().startswith("python"):
            child_env = os.environ.copy()
            child_env["PYTHONUTF8"] = "1"
            child_env["PYTHONIOENCODING"] = "utf-8"
        try:
            completed = subprocess.run(
                adapter.command,
                input=json.dumps(request, ensure_ascii=False),
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=adapter.timeout_seconds,
                cwd=workspace,
                env=child_env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"Расширение {name} превысило время ожидания.") from exc
        if completed.returncode:
            raise ValueError(f"Расширение {name} завершилось с ошибкой: {completed.stderr[-2000:]}")
        if len(completed.stdout) > 2_000_000:
            raise ValueError("Расширение вернуло слишком большой JSON; сохраните данные как артефакт.")
        result = ExtensionResult.model_validate_json(completed.stdout)
    hashes = {}
    for artifact in result.artifacts:
        path = contained(workspace, artifact)
        if not path.is_relative_to((workspace / "assets").resolve()) and not path.is_relative_to(
            (workspace / "output").resolve()
        ):
            raise ValueError("Артефакты расширений должны находиться в assets/ или output/.")
        hashes[artifact] = digest(path)
    write_json(cache, {"result": result.model_dump(), "hashes": hashes})
    return result

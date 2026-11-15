import os

import pytest

from codexdoc.library import Library
from codexdoc.models import AgentProfile, Metadata
from codexdoc.storage import write_json
from codexdoc.workspaces import create_workspace, example_content, load_workspace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def library(tmp_path):
    return Library(tmp_path / "library")


@pytest.fixture
def workspace(tmp_path, library):
    path = create_workspace(
        tmp_path / "work",
        Metadata(title="Проверка"),
        library.get_template("lab"),
        AgentProfile(),
        [],
        library=library,
    )
    write_json(path / "document.json", example_content())
    # These fixtures exercise rendering, not an agent's semantic judgement.
    manifest, _, _, _ = load_workspace(path)
    manifest.content_review_required = False
    write_json(path / "workspace.json", manifest)
    return path

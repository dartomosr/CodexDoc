"""Stable entrypoint used by generated workspaces, from any current directory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codexdoc.cli import main  # noqa: E402

workspace = sys.argv[1]
arguments = sys.argv[2:] or ["inspect"]
raise SystemExit(main([arguments[0], workspace, *arguments[1:]]))

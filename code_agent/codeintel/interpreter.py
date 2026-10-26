"""Choose a project's existing Python environment without running/installing it."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from code_agent.codeintel.paths import ProjectPathError, safe_child


def select_python(root: str | Path) -> Path:
    directory, name = (".venv/Scripts", "python.exe") if os.name == "nt" else (".venv/bin", "python")
    try:
        # A virtualenv's bin/python normally points to a system Python. Keep
        # that executable symlink while preventing an external .venv directory.
        parent = safe_child(root, directory)
    except ProjectPathError:
        return Path(sys.executable)
    executable = parent / name
    if executable.is_file() and (os.name == "nt" or os.access(executable, os.X_OK)):
        return executable
    return Path(sys.executable)


def interpreter_info(root: str | Path) -> dict:
    python = select_python(root)
    return {"python": str(python), "interpreter_source": "backend" if os.path.normcase(str(python)) == os.path.normcase(sys.executable) else "project-venv"}

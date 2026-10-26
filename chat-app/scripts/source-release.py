"""Create a reproducible source release from the standalone project's allowlist.

Run from any directory: python scripts/source-release.py --output /path/release.zip
The archive contains source/configuration templates, never installed dependencies,
compiled bundles, local databases, access tokens, or runtime conversation data.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT_FILES = frozenset({
    "package.json", "package-lock.json", "pyproject.toml", "README.md", "LICENSE", "NOTICE",
    "THIRD_PARTY_NOTICES.md", "CONTRIBUTING.md", "SECURITY.md", ".env.example", ".gitignore",
    "Dockerfile", "compose.yml", "compose.yaml", "vite.config.js", "index.html",
})
SOURCE_DIRS = frozenset({"src", "chat_agent", "backend_tests", "tests", "scripts", "licenses", ".github"})
EXCLUDED_DIRS = frozenset({
    "__pycache__", "node_modules", ".venv", "venv", "build", "dist", ".git", ".pytest_cache",
    ".ruff_cache", ".vite", ".cache", ".mypy_cache", "coverage", "htmlcov", "data", "artifacts", "workspaces",
})
SOURCE_SUFFIXES = frozenset({
    ".py", ".js", ".mjs", ".cjs", ".ts", ".vue", ".css", ".scss", ".html", ".json", ".toml",
    ".yaml", ".yml", ".md", ".txt", ".svg", ".png", ".webp", ".jpg", ".jpeg", ".ico",
})
REQUIRED_FILES = frozenset({"pyproject.toml", "package.json", "package-lock.json", "README.md", "LICENSE", "NOTICE", "index.html", "vite.config.js", "chat_agent/__init__.py", "src/main.js"})


def _is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(os.path, "isjunction", lambda _: False)(path)


def _allowed_file(relative: Path) -> bool:
    name = relative.name.lower()
    if name == ".env" or name.endswith(".env") or name.startswith(".env."):
        return relative.as_posix() == ".env.example"
    if any(component in EXCLUDED_DIRS or component.endswith(".egg-info") for component in relative.parts):
        return False
    if name.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".pyc", ".pyo", ".key", ".pem", ".log")):
        return False
    return relative.as_posix() in ROOT_FILES or relative.suffix.lower() in SOURCE_SUFFIXES


def source_files(root: Path) -> list[Path]:
    root = root.resolve()
    selected: set[Path] = set()
    for name in ROOT_FILES:
        path = root / name
        if path.is_file() and not _is_link(path):
            selected.add(path)
    for name in SOURCE_DIRS:
        directory = root / name
        if not directory.is_dir() or _is_link(directory):
            continue
        for current, directories, names in os.walk(directory, followlinks=False):
            current = Path(current)
            directories[:] = [name for name in directories if name not in EXCLUDED_DIRS and not name.endswith(".egg-info") and not _is_link(current / name)]
            for filename in names:
                path = current / filename
                if _is_link(path) or not path.is_file() or not path.resolve().is_relative_to(root):
                    continue
                if _allowed_file(path.relative_to(root)):
                    selected.add(path)
    files = sorted(selected, key=lambda path: path.relative_to(root).as_posix())
    missing = REQUIRED_FILES - {path.relative_to(root).as_posix() for path in files}
    if missing:
        raise ValueError("Source release is incomplete: " + ", ".join(sorted(missing)))
    return files


def build_release(root: Path, output: Path) -> dict:
    root = root.resolve()
    output = output.absolute()
    if _is_link(output):
        raise ValueError("Release output cannot be a filesystem link")
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(metadata["project"]["version"])
    if not re.fullmatch(r"[A-Za-z0-9.+_-]+", version):
        raise ValueError("Invalid release version")
    if metadata["project"].get("license") != "Apache-2.0":
        raise ValueError("The standalone source release must declare Apache-2.0")
    files = source_files(root)
    prefix = f"chat-agent-{version}"
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=".chat-release-", suffix=".zip", dir=output.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in files:
                relative = path.relative_to(root).as_posix()
                info = zipfile.ZipInfo(f"{prefix}/{relative}", date_time=(2000, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"output": str(output), "version": version, "root": prefix, "files": len(files), "bytes": output.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an independent Apache-2.0 source ZIP without local data or installed dependencies")
    parser.add_argument("--output", required=True, type=Path, help="Destination ZIP path")
    args = parser.parse_args()
    try:
        result = build_release(Path(__file__).resolve().parents[1], args.output)
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

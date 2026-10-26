"""Source releases contain both independent applications and no local runtime data."""
import ast
import importlib.util
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("source_release", ROOT / "scripts" / "source-release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def write_fixture(root: Path):
    for name in release.REQUIRED_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname="chat-agent-standalone"\nversion="0.1.0"\nlicense="Apache-2.0"\n', encoding="utf-8")


def test_release_allowlist_excludes_secrets_builds_dependencies_and_links(tmp_path):
    root = tmp_path / "source"
    write_fixture(root)
    allowed = ["src/App.vue", "chat_agent/api.py", "backend_tests/test_api.py", "tests/check.mjs", "scripts/source-release.py", "licenses/vue.txt", ".github/workflows/check.yml", ".env.example", ".gitignore"]
    forbidden = [".env", ".env.local", "private-chat.json", "chat.sqlite3", "src/.env", "src/.env.production", "src/private.env", "src/access.key", "src/models.sqlite3", "src/runtime.db-wal", "chat_agent/__pycache__/api.pyc", "chat_agent/build/copied.py", "src/node_modules/package/index.js", "src/dist/bundle.js", "scripts/.venv/installed.py", "src/artifacts/user.txt", "src/workspaces/session.json", "logs/private.txt", "conversation-data/private.json", ".venv/pyvenv.cfg", "node_modules/private.js", "dist/index.html", "build/lib/chat_agent/server.py"]
    for name in allowed + forbidden:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public" if name in allowed else "private", encoding="utf-8")
    output = tmp_path / "source.zip"
    result = release.build_release(root, output)
    with zipfile.ZipFile(output) as archive:
        names = {name.removeprefix(result["root"] + "/") for name in archive.namelist()}
    assert set(allowed).issubset(names)
    assert not set(forbidden) & names
    assert release.REQUIRED_FILES.issubset(names)
    first = output.read_bytes()
    release.build_release(root, output)
    assert output.read_bytes() == first


def test_real_release_contains_apache_sources_and_no_coding_imports(tmp_path):
    output = tmp_path / "real-source.zip"
    report = release.build_release(ROOT, output)
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        prefix = report["root"] + "/"
        assert "Apache License" in archive.read(prefix + "LICENSE").decode()
        assert "Apache-2.0" in archive.read(prefix + "pyproject.toml").decode()
        assert prefix + "src/App.vue" in names
        assert prefix + "chat_agent/server.py" in names
        assert prefix + ".env.example" in names
        for name in names:
            assert name.startswith(prefix)
            assert ".." not in Path(name).parts
            assert not any(part in release.EXCLUDED_DIRS for part in Path(name).parts)
            assert not name.endswith((".db", ".sqlite3", ".pyc"))
            if name.startswith(prefix + "chat_agent/") and name.endswith(".py"):
                tree = ast.parse(archive.read(name).decode("utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        assert all(alias.name.split(".")[0] not in {"code_agent", "ch_agent"} for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        assert node.module.split(".")[0] not in {"code_agent", "ch_agent"}


def test_source_release_rejects_incomplete_project(tmp_path):
    root = tmp_path / "missing"
    root.mkdir()
    with pytest.raises(ValueError, match="incomplete"):
        release.source_files(root)


def test_source_release_never_follows_source_links(tmp_path):
    root = tmp_path / "linked-source"
    write_fixture(root)
    outside = tmp_path / "private-files"
    outside.mkdir()
    (outside / "private.js").write_text("private", encoding="utf-8")
    try:
        (root / "src" / "linked-file.js").symlink_to(outside / "private.js")
        (root / "src" / "linked-directory").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit creating symlinks for this test account")
    output = tmp_path / "linked-source.zip"
    report = release.build_release(root, output)
    with zipfile.ZipFile(output) as archive:
        assert not any("linked-file" in name or "linked-directory" in name for name in archive.namelist())
        assert len(archive.namelist()) == report["files"]

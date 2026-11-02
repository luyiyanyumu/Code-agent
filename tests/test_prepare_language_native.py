"""Build-time archive checks use tiny fixtures and do not install any tools."""
import hashlib
import importlib.util
from pathlib import Path
import stat
import zipfile

import pytest

spec = importlib.util.spec_from_file_location("prepare_language_native", Path(__file__).parents[1] / "scripts/prepare_language_native.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


def test_native_archive_keeps_the_runtime_tree_and_licenses(tmp_path):
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("sdk/bin/server.exe", b"fixture executable")
        package.writestr("sdk/LICENSE", b"fixture notice")
    native.extract_zip(archive, tmp_path / "expanded", "sdk")
    assert (tmp_path / "expanded/bin/server.exe").read_bytes() == b"fixture executable"
    assert (tmp_path / "expanded/LICENSE").read_bytes() == b"fixture notice"


@pytest.mark.parametrize("name", ["../escape", "sdk/../../escape", "/absolute", "C:/absolute", "sdk\\..\\escape"])
def test_native_archive_rejects_escape_paths_before_writing_them(tmp_path, name):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(name, b"must not escape")
    with pytest.raises(ValueError):
        native.extract_zip(archive, tmp_path / "expanded")
    assert not (tmp_path / "escape").exists()


def test_native_archive_rejects_symlink_members(tmp_path):
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as package:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        package.writestr(info, "../../unrelated")
    with pytest.raises(ValueError, match="links"):
        native.extract_zip(archive, tmp_path / "expanded")


def test_changed_cached_input_is_rejected_without_network(tmp_path):
    source = tmp_path / "tool.zip"
    source.write_bytes(b"changed")
    record = {"file": source.name, "sha256": hashlib.sha256(b"original").hexdigest(), "url": "https://example.invalid/tool.zip"}
    with pytest.raises(ValueError, match="changed pinned input"):
        native.acquire(record, tmp_path, download=False)

"""Small original-archive fixtures for the standalone Windows Java packager."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import zipfile

import pytest

SPEC = importlib.util.spec_from_file_location("prepare_windows_java", Path(__file__).parents[1] / "scripts/prepare_windows_java.py")
packager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager)


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    cache = tmp_path / "archive cache 中文 with spaces"
    cache.mkdir()
    jdk, jdt, lombok = [cache / entry[0] for entry in packager.INPUTS]
    with zipfile.ZipFile(jdk, "w") as archive:
        for name in ["bin/java.exe", "bin/javac.exe", "release", "NOTICE", "legal/java.base/LICENSE"]:
            archive.writestr("jdk-21.0.12.1+1/" + name, "fixture " + name)
    with tarfile.open(jdt, "w:gz") as archive:
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for name in ["bin/jdtls", "bin/jdtls.py", "config_win/config.ini", "plugins/org.eclipse.equinox.launcher_fixture.jar", "features/license.html"]:
            content = ("fixture " + name).encode()
            member = tarfile.TarInfo("./" + name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    with zipfile.ZipFile(lombok, "w") as archive:
        archive.writestr("LICENSE", "fixture original Lombok license")
    monkeypatch.setattr(packager, "INPUTS", tuple((path.name, packager.sha(path)) for path in [jdk, jdt, lombok]))
    return cache


def test_original_archives_prepare_relocatable_tools_and_preserve_notices(inputs, tmp_path):
    payload = tmp_path / "isolated installation 中文"
    payload.mkdir()
    original = {"schemaVersion": 1, "version": "fixture", "node": "preserved", "runtimeVersions": {"node": "kept"}}
    (payload / "desktop-runtime.json").write_text(json.dumps(original))
    (payload / "package.json").write_text("{}")
    result = packager.prepare(inputs, payload)
    assert result["prepared"]
    assert (payload / "runtimes/java/legal/java.base/LICENSE").read_text() == "fixture legal/java.base/LICENSE"
    assert (payload / "runtimes/jdtls/features/license.html").read_text() == "fixture features/license.html"
    assert (payload / "runtimes/lombok/LICENSE.txt").read_text() == "fixture original Lombok license"
    manifest = json.loads((payload / "desktop-runtime.json").read_text())
    assert {key: manifest[key] for key in packager.PATHS} == packager.PATHS
    assert manifest["node"] == "preserved" and manifest["runtimeVersions"]["node"] == "kept"
    assert not any(str(inputs) in value for value in manifest.values() if isinstance(value, str))
    before = packager.inventory(payload)
    assert packager.prepare(inputs, payload)["files"] == result["files"]
    assert packager.inventory(payload) == before, "Resume must preserve identical installed files"


def test_corrupt_input_is_never_extracted(inputs):
    (inputs / packager.INPUTS[0][0]).write_bytes(b"corrupt cached archive")
    with pytest.raises(ValueError, match="checksum"):
        packager.prepare(inputs)
    assert not list(inputs.glob("java-tools-*"))


def test_modified_extracted_cache_is_not_silently_trusted(inputs):
    result = packager.prepare(inputs)
    (Path(result["tools"]) / "java/bin/java.exe").write_text("changed tool")
    with pytest.raises(ValueError, match="cache changed"):
        packager.prepare(inputs)


def test_unknown_files_in_existing_payload_are_preserved_and_rejected(inputs, tmp_path):
    payload = tmp_path / "payload"
    (payload / "runtimes/java").mkdir(parents=True)
    unknown = payload / "runtimes/java/user-file.txt"
    unknown.write_text("keep")
    (payload / "desktop-runtime.json").write_text('{"schemaVersion":1}')
    (payload / "package.json").write_text("{}")
    with pytest.raises(ValueError, match="fresh payload"):
        packager.prepare(inputs, payload)
    assert unknown.read_text() == "keep"


def test_zip_traversal_never_reaches_outside_preparation_directory(inputs, monkeypatch):
    archive = inputs / packager.INPUTS[0][0]
    with zipfile.ZipFile(archive, "a") as target:
        target.writestr("jdk-21.0.12.1+1/../../escaped.txt", "must not write")
    monkeypatch.setattr(packager, "INPUTS", tuple((name, packager.sha(inputs / name)) for name, _ in packager.INPUTS))
    with pytest.raises(ValueError, match="Unsafe"):
        packager.prepare(inputs)
    assert not (inputs / "escaped.txt").exists()

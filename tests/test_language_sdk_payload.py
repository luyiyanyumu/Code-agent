"""SDK input authenticity and archive boundary checks; never install a toolchain."""
import base64
import hashlib
import importlib.util
from pathlib import Path
import tarfile
import io
import zipfile

import pytest

spec = importlib.util.spec_from_file_location("prepare_language_sdks", Path(__file__).parents[1] / "scripts/prepare_language_sdks.py")
sdk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sdk)


def test_zip_module_hash_matches_the_go_hash1_definition(tmp_path):
    archive = tmp_path / "module.zip"
    values = {"example.com/public@v1.0.0/go.mod": b"module example.com/public\n", "example.com/public@v1.0.0/a.go": b"package public\n"}
    with zipfile.ZipFile(archive, "w") as output:
        for name, value in reversed(list(values.items())):
            output.writestr(name, value)
    summary = b"".join((hashlib.sha256(values[name]).hexdigest() + "  " + name + "\n").encode() for name in sorted(values))
    expected = "h1:" + base64.b64encode(hashlib.sha256(summary).digest()).decode()
    assert sdk.zip_module_sum(archive) == expected
    with zipfile.ZipFile(archive, "a") as output:
        output.writestr("example.com/public@v1.0.0/extra.go", b"changed")
    assert sdk.zip_module_sum(archive) != expected


@pytest.mark.parametrize("name", ["../outside", "/outside", "C:/outside", "root/../../outside", "root\\..\\..\\outside"])
def test_sdk_zip_rejects_path_escape_before_writing(tmp_path, name):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, b"never published")
    with pytest.raises(ValueError, match="Unsafe"):
        sdk.extract_zip(archive, tmp_path / "output")
    assert not (tmp_path / "outside").exists()


def test_tool_package_preserves_notices_and_places_only_its_portable_tools_in_the_runtime(tmp_path):
    archive = tmp_path / "server.nupkg"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("tools/net10.0/any/Server.dll", b"binary fixture")
        output.writestr("tools/net10.0/any/BuildHost/Host.dll", b"host fixture")
        output.writestr("LICENSE.txt", b"original notices")
        output.writestr("server.nuspec", b"original package identity")
    sdk.extract_zip(archive, tmp_path / "runtime", prefix="tools/net10.0/any")
    sdk.extract_zip(archive, tmp_path / "metadata", exclude=("tools",))
    assert (tmp_path / "runtime/BuildHost/Host.dll").read_bytes() == b"host fixture"
    assert (tmp_path / "metadata/LICENSE.txt").read_bytes() == b"original notices"
    assert not (tmp_path / "metadata/tools").exists()


def test_rust_component_overlay_keeps_original_distribution_notices(tmp_path):
    archive = tmp_path / "rust-src.tar.xz"
    with tarfile.open(archive, "w:xz") as output:
        for name, content in {"rust-src-fixture/rust-src/lib/rustlib/src/rust/library/std/src/lib.rs": b"source",
                              "rust-src-fixture/LICENSE-APACHE": b"license"}.items():
            record = tarfile.TarInfo(name)
            record.size = len(content)
            output.addfile(record, io.BytesIO(content))
    sdk.extract_rust(archive, tmp_path / "rust", "rust-src")
    assert (tmp_path / "rust/lib/rustlib/src/rust/library/std/src/lib.rs").read_bytes() == b"source"
    assert (tmp_path / "rust/distribution-manifests/rust-src/LICENSE-APACHE").read_bytes() == b"license"


def test_prune_removes_only_the_named_nonruntime_go_fixture(tmp_path):
    unwanted = tmp_path / sdk.PRUNED_TEST_FIXTURES[0]
    unwanted.parent.mkdir(parents=True)
    unwanted.write_text("official cmd/go test data")
    neighboring = unwanted.with_name("keep.go")
    neighboring.write_text("package keep")
    other = tmp_path / "runtimes/language-servers/go/keep.log"
    other.write_text("not covered by this precise rule")
    sdk.prune_official_test_fixtures(tmp_path)
    assert not unwanted.exists()
    assert neighboring.read_text() == "package keep"
    assert other.exists()

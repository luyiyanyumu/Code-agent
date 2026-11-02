"""Exercise the production archive validator without installing or touching services."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build-windows-installer.ps1"
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows PowerShell required")

CRITICAL = (
    "npm/release.json", "bin/code-agent-desktop-service.mjs", "npm/launcher-service.mjs",
    "web/dist/index.html", "code_agent/server.py", "code_agent/harness/node/runner.mjs",
    "runtimes/node/node.exe", "runtimes/python/python.exe",
    "runtimes/webview2/msedgewebview2.exe", "Code-agent Launcher.exe", "Code-agent Uninstall.exe",
)


def make_payload(parent: Path) -> Path:
    payload = parent / "payload with spaces"
    payload.mkdir()
    for name in CRITICAL:
        target = payload / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(("fixture: " + name).encode())
    (payload / "package.json").write_text(json.dumps({"version": "0.10.2"}), encoding="utf-8")
    (payload / "desktop-runtime.json").write_text(json.dumps({
        "schemaVersion": 1, "version": "0.10.2", "node": "runtimes/node/node.exe",
        "python": "runtimes/python/python.exe", "webview2": "runtimes/webview2",
        "runtimeVersions": {"node": "24.14.0", "python": "3.13.7"},
    }), encoding="utf-8")
    return payload


def archive_payload(payload: Path, archive: Path, *, extra: zipfile.ZipInfo | str | None = None) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(payload).as_posix())
        if extra is not None:
            bundle.writestr(extra, b"unsafe fixture")


def validator(tmp_path: Path, payload: Path, archive: Path) -> subprocess.CompletedProcess[str]:
    helpers = BUILDER.read_text(encoding="utf-8-sig").split("# Build starts here.", 1)[0]
    driver = tmp_path / "validate fixture.ps1"
    driver.write_text(helpers + """
$payload = [System.IO.Path]::GetFullPath($PayloadDirectory).TrimEnd('\\')
$files = @(Get-ChildItem -LiteralPath $payload -File -Recurse -Force)
Assert-ReusablePayloadArchive -ArchivePath $ReusePayloadArchive -PayloadRoot $payload -ExpectedFiles $files -ExpectedVersion '0.10.2' | ConvertTo-Json -Compress
""", encoding="utf-8")
    return subprocess.run([
        POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(driver),
        "-PayloadDirectory", str(payload), "-ReusePayloadArchive", str(archive),
        "-Root", str(ROOT),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40)


def test_archive_reuse_checks_every_file_and_reports_digest(tmp_path: Path) -> None:
    payload = make_payload(tmp_path)
    archive = tmp_path / "existing payload.zip"
    archive_payload(payload, archive)
    result = validator(tmp_path, payload, archive)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["files"] == len(CRITICAL) + 2
    assert report["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert report["bytes"] == sum(p.stat().st_size for p in payload.rglob("*") if p.is_file())


def test_content_hash_verification_really_uses_multiple_bounded_workers(tmp_path: Path) -> None:
    payload = make_payload(tmp_path)
    folder = payload / "web/concurrent"
    folder.mkdir()
    for index in range(192):
        (folder / f"asset-{index:03}.bin").write_bytes(hashlib.shake_256(str(index).encode()).digest(128 * 1024))
    archive = tmp_path / "concurrent.zip"
    archive_payload(payload, archive)
    result = validator(tmp_path, payload, archive)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["verificationWorkers"] == 4
    assert 2 <= report["verificationMaxConcurrentFiles"] <= 4
    assert report["verificationComparedBytes"] == 2 * report["bytes"]
    assert report["verificationPeakWorkingSetBytes"] < 2 * 1024**3
    (tmp_path / "concurrency-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


@pytest.mark.parametrize("kind", ["extra", "duplicate", "reparse"])
def test_parallel_verification_preserves_structural_rejection(tmp_path: Path, kind: str) -> None:
    payload = make_payload(tmp_path)
    archive = tmp_path / "invalid.zip"
    if kind == "reparse":
        entry = zipfile.ZipInfo("web/reparse.js")
        entry.create_system = 0
        entry.external_attr = 0x400
        expected = "Unsafe reusable archive input"
    else:
        entry = "web/dist/index.html" if kind == "duplicate" else "web/extra.js"
        expected = "Duplicate reusable archive path" if kind == "duplicate" else "unexpected input"
    archive_payload(payload, archive, extra=entry)
    result = validator(tmp_path, payload, archive)
    assert result.returncode != 0
    assert expected in result.stderr, result.stdout + result.stderr


def test_locked_sql_license_node_modules_names_have_a_narrow_scope(tmp_path: Path) -> None:
    cases = {
        "runtimes/language-servers/sql/licenses/@google-cloud/common/node_modules": False,
        "runtimes/language-servers/sql/licenses/@google-cloud/common/node_modules/dependency/LICENSE": False,
        "runtimes/language-servers/sql/node_modules/dependency/LICENSE": True,
        "runtimes/language-servers/sql/licenses-other/node_modules/LICENSE": True,
        "project/node_modules/dependency/LICENSE": True,
        "node_modules/dependency/LICENSE": True,
        "runtimes/node/node_modules/dependency/LICENSE": False,
        "runtimes/language-servers/node/node_modules/dependency/LICENSE": False,
        "runtimes/language-servers/sql/licenses/pkg/node_modules/.env": True,
    }
    helpers = BUILDER.read_text(encoding="utf-8-sig").split("# Build starts here.", 1)[0]
    inputs = tmp_path / "scoped paths.json"
    inputs.write_text(json.dumps(list(cases)), encoding="utf-8")
    driver = tmp_path / "check scoped names.ps1"
    driver.write_text(helpers + """
$paths = Get-Content -LiteralPath $Root -Raw | ConvertFrom-Json
@($paths | ForEach-Object { [pscustomobject]@{ path = $_; unsafe = [bool](Test-UnsafePayloadName $_) } }) | ConvertTo-Json -Compress
""", encoding="utf-8")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(driver),
                             "-PayloadDirectory", str(tmp_path), "-Root", str(inputs)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert {row["path"]: row["unsafe"] for row in json.loads(result.stdout)} == cases


def test_archive_reuse_rejects_wrong_runtime_version(tmp_path: Path) -> None:
    payload = make_payload(tmp_path)
    manifest = payload / "desktop-runtime.json"
    value = json.loads(manifest.read_text())
    value["version"] = "0.10.1"
    manifest.write_text(json.dumps(value))
    archive = tmp_path / "wrong-version.zip"
    archive_payload(payload, archive)
    result = validator(tmp_path, payload, archive)
    assert result.returncode != 0
    assert "version or layout" in result.stderr


def test_archive_reuse_rejects_same_length_modified_asset(tmp_path: Path) -> None:
    payload = make_payload(tmp_path)
    archive = tmp_path / "outdated.zip"
    archive_payload(payload, archive)
    asset = payload / "web/dist/index.html"
    original = asset.read_bytes()
    asset.write_bytes(b"x" + original[1:])
    result = validator(tmp_path, payload, archive)
    assert result.returncode != 0
    assert "content differs from staging: web/dist/index.html" in result.stderr


@pytest.mark.parametrize("name", ["../escape.txt", "C:/absolute.txt", "web\\escape.txt", ".env", "logs/private.txt", "data.sqlite", "runtimes/python/Lib/site-packages/Scripts/pip.exe"])
def test_archive_reuse_rejects_unsafe_entries(tmp_path: Path, name: str) -> None:
    payload = make_payload(tmp_path)
    archive = tmp_path / "unsafe.zip"
    archive_payload(payload, archive, extra=name)
    if "\\" in name:
        # ZipInfo canonicalizes Windows separators while writing; restore the
        # raw central/local header spelling to exercise the reader's guard.
        archive.write_bytes(archive.read_bytes().replace(name.replace("\\", "/").encode(), name.encode()))
    result = validator(tmp_path, payload, archive)
    assert result.returncode != 0
    assert "Unsafe reusable archive input" in result.stderr


def test_archive_reuse_rejects_symlink_entry(tmp_path: Path) -> None:
    payload = make_payload(tmp_path)
    archive = tmp_path / "symlink.zip"
    link = zipfile.ZipInfo("linked-file")
    link.create_system = 3
    link.external_attr = 0o120777 << 16
    archive_payload(payload, archive, extra=link)
    result = validator(tmp_path, payload, archive)
    assert result.returncode != 0
    assert "Unsafe reusable archive input: linked-file" in result.stderr


def test_archive_reuse_rejects_missing_member(tmp_path: Path) -> None:
    payload = make_payload(tmp_path)
    archive = tmp_path / "incomplete.zip"
    archive_payload(payload, archive)
    (payload / "new-asset.txt").write_text("asset")
    result = validator(tmp_path, payload, archive)
    assert result.returncode != 0
    assert "membership differs" in result.stderr


def add_java_payload(payload: Path) -> None:
    manifest_path = payload / "desktop-runtime.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(java="runtimes/java/bin/java.exe", jdtls="runtimes/jdtls", lombok="runtimes/lombok/lombok.jar")
    manifest_path.write_text(json.dumps(manifest))
    for name in [manifest["java"], manifest["lombok"], "runtimes/java/bin/javac.exe", "runtimes/java/release", "runtimes/java/NOTICE",
                 "runtimes/java/legal/java.base/LICENSE", "runtimes/jdtls/bin/jdtls", "runtimes/jdtls/bin/jdtls.py",
                 "runtimes/jdtls/config_win/config.ini", "runtimes/jdtls/plugins/org.eclipse.equinox.launcher_fixture.jar"]:
        path = payload / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture Java dependency")


def test_archive_reuse_accepts_complete_java_and_checks_its_file_content(tmp_path: Path) -> None:
    payload = make_payload(tmp_path)
    add_java_payload(payload)
    archive = tmp_path / "java.zip"
    archive_payload(payload, archive)
    result = validator(tmp_path, payload, archive)
    assert result.returncode == 0, result.stdout + result.stderr
    (payload / "runtimes/java/bin/java.exe").write_text("x" * len("fixture Java dependency"))
    result = validator(tmp_path, payload, archive)
    assert result.returncode != 0 and "content differs" in result.stderr


@pytest.mark.parametrize("missing", ["runtimes/java/bin/javac.exe", "runtimes/jdtls/bin/jdtls.py", "runtimes/jdtls/plugins/org.eclipse.equinox.launcher_fixture.jar"])
def test_archive_reuse_rejects_declared_but_incomplete_java(tmp_path: Path, missing: str) -> None:
    payload = make_payload(tmp_path)
    add_java_payload(payload)
    (payload / missing).unlink()
    archive = tmp_path / "incomplete-java.zip"
    archive_payload(payload, archive)
    result = validator(tmp_path, payload, archive)
    assert result.returncode != 0 and "Java" in result.stderr


def test_archive_reuse_rejects_external_java_manifest_paths(tmp_path: Path) -> None:
    payload = make_payload(tmp_path)
    add_java_payload(payload)
    path = payload / "desktop-runtime.json"
    value = json.loads(path.read_text())
    value["java"] = "../external/java.exe"
    path.write_text(json.dumps(value))
    archive = tmp_path / "external-java.zip"
    archive_payload(payload, archive)
    result = validator(tmp_path, payload, archive)
    assert result.returncode != 0 and "layout" in result.stderr


@pytest.mark.parametrize("appended", [False, True], ids=["embedded", "appended"])
def test_repack_only_compiles_setup_and_preserves_payload(tmp_path: Path, appended: bool) -> None:
    """Compile a tiny actual installer; fail if either staged executable is rebuilt."""
    csc = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not csc.is_file():
        pytest.skip("native .NET Framework compiler unavailable")
    payload = make_payload(tmp_path)
    native_fixture = tmp_path / "NativeFixture.cs"
    native_fixture.write_text("internal static class NativeFixture { static void Main() {} }")
    compiled = subprocess.run([str(csc), "/nologo", "/target:winexe", "/platform:x64",
                               f"/out:{payload / 'Code-agent Launcher.exe'}", str(native_fixture)],
                              capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    archive = tmp_path / "verified payload.zip"
    archive_payload(payload, archive)
    before = {p.relative_to(payload).as_posix(): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in payload.rglob("*") if p.is_file()}
    source = tmp_path / "source fixture"
    (source / "desktop/installer").mkdir(parents=True)
    (source / "scripts").mkdir()
    (source / "package.json").write_text('{"version":"0.10.2"}')
    (source / "scripts/build-launcher-exe.ps1").write_text("throw 'The launcher must never be rebuilt during archive reuse.'")
    (source / "desktop/installer/app.manifest").write_text('''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0"><assemblyIdentity version="0.10.2.0" name="CodeAgent.Repack.Fixture"/></assembly>''')
    (source / "desktop/installer/Program.cs").write_text("namespace CodeAgent.Installer { internal static class SetupProgram { static void Main() {} } }")
    (source / "desktop/installer/Uninstall.cs").write_text("namespace CodeAgent.Installer { internal static class UninstallProgram { static void Main() {} } }")
    shutil.copyfile(ROOT / "desktop/installer/PayloadSource.cs", source / "desktop/installer/PayloadSource.cs")
    shutil.copyfile(ROOT / "desktop/installer/PayloadArchiveVerifier.cs", source / "desktop/installer/PayloadArchiveVerifier.cs")
    output = tmp_path / "installer output"
    result = subprocess.run([
        POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(BUILDER),
        "-Root", str(source), "-PayloadDirectory", str(payload), "-OutputDirectory", str(output),
        "-ReusePayloadArchive", str(archive),
    ] + (["-ForceAppendedPayload"] if appended else []), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    metadata = json.loads((output / "Code-agent-Setup-0.10.2-win-x64.exe.json").read_text(encoding="utf-8-sig"))
    assert metadata["reusedPayloadArchive"] is True
    assert metadata["payloadArchiveSha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert metadata["payloadFormat"] == ("appended-zip-v1" if appended else "embedded-resource")
    assert (metadata["payloadOffset"] is not None) == appended
    after = {p.relative_to(payload).as_posix(): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in payload.rglob("*") if p.is_file()}
    assert before == after

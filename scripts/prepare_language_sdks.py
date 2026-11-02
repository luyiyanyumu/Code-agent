"""Prepare pinned portable Go, Rust and .NET language-analysis SDKs.

All package files are original distributions or a fixed, Go-sumdb-verified
gopls source build. No system SDK, profile, registry or user configuration is
modified. Output is merged into the application by the main payload builder.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import time
import urllib.request
import uuid
import zipfile

LOCK = Path(__file__).with_name("language_sdk_inputs.json")
PRUNED_TEST_FIXTURES = ("runtimes/language-servers/go/src/cmd/go/internal/modindex/testdata/ignore_non_source/baz.log",)


def emit(stage, **details):
    print(json.dumps({"stage": stage, **details}, ensure_ascii=False), flush=True)


def digest(path, algorithm="sha256"):
    with path.open("rb") as source:
        return hashlib.file_digest(source, algorithm).hexdigest()


def download(record, cache, allow):
    path = cache / record["file"]
    algorithm = "sha512" if record.get("sha512") else "sha256"
    expected = record[algorithm]
    if path.is_file() and digest(path, algorithm) == expected:
        emit("cached", id=record["id"], bytes=path.stat().st_size)
        return path
    if not allow:
        raise ValueError("Missing or invalid pinned SDK input: " + record["id"])
    for url in [record["url"], *record.get("mirrors", [])]:
        partial = cache / (record["file"] + "." + uuid.uuid4().hex + ".partial")
        emit("download", id=record["id"], transport=url)
        request = urllib.request.Request(url, headers={"User-Agent": "Code-agent-language-payload/1"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as target:
                count, last = 0, time.monotonic()
                while chunk := response.read(1024 * 1024):
                    target.write(chunk)
                    count += len(chunk)
                    if time.monotonic() - last >= 10:
                        emit("downloading", id=record["id"], bytes=count)
                        last = time.monotonic()
            if digest(partial, algorithm) != expected:
                raise ValueError("Official SDK checksum differs")
            partial.replace(path)
            emit("downloaded", id=record["id"], bytes=path.stat().st_size, transport=url)
            return path
        except (OSError, ValueError) as error:
            # Mirrors only transport the exact original bytes. The official
            # pinned digest remains mandatory for every accepted response.
            emit("transport-rejected", id=record["id"], kind=type(error).__name__)
    raise ValueError("No transport supplied the pinned SDK input: " + record["id"])


def member(name):
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in name or not path.parts:
        raise ValueError("Unsafe SDK archive path")
    return path.parts


def extract_zip(archive, target, strip=None, prefix=None, exclude=()):
    with zipfile.ZipFile(archive) as source:
        for item in source.infolist():
            parts = member(item.filename)
            if parts[0] in exclude:
                continue
            if strip:
                if parts[0] != strip:
                    raise ValueError("Unexpected SDK archive root")
                parts = parts[1:]
            if prefix:
                expected = tuple(prefix.split("/"))
                if parts[:len(expected)] != expected:
                    continue
                parts = parts[len(expected):]
            if not parts:
                continue
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("SDK archive contains a filesystem link")
            output = target.joinpath(*parts)
            if item.is_dir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with source.open(item) as stream, output.open("wb") as destination:
                    shutil.copyfileobj(stream, destination)


def extract_rust(archive, target, component):
    with tarfile.open(archive, "r:xz") as source:
        for item in source:
            if item.isdir() and item.name in {".", "./"}:
                continue
            parts = member(item.name)
            if len(parts) < 2:
                continue
            if not item.isfile() and not item.isdir():
                raise ValueError("Rust archive contains a filesystem link")
            if parts[1] == component:
                relative = parts[2:]
            elif len(parts) == 2 and item.isfile():
                relative = ("distribution-manifests", component, parts[-1])
            else:
                continue
            if not relative:
                continue
            output = target.joinpath(*relative)
            if item.isdir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(item) as stream, output.open("wb") as destination:
                    shutil.copyfileobj(stream, destination)


def inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or path.is_junction():
            raise ValueError("SDK output contains a filesystem link")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = digest(path)
    return result


def prune_official_test_fixtures(root):
    # This exact upstream cmd/go test sample is not a runtime input. Keep the
    # installer's global prohibition on logs rather than broadening it for SDKs.
    for name in PRUNED_TEST_FIXTURES:
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink() or path.is_junction():
            raise ValueError("SDK test-fixture path escaped the prepared tree")
        if path.exists():
            path.unlink()


def zip_module_sum(path):
    """Go's documented dirhash.HashZip/Hash1, independent of ZIP encoding."""
    summary = hashlib.sha256()
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if "\n" in name:
                raise ValueError("Invalid Go module ZIP filename")
            with archive.open(name) as source:
                value = hashlib.file_digest(source, "sha256").hexdigest()
            summary.update((value + "  " + name + "\n").encode())
    return "h1:" + base64.b64encode(summary.digest()).decode()


def seed_go_modules(build_home, spec):
    """Reuse only public cached modules authenticated by pinned gopls/go.sum."""
    source = Path.home() / "go/pkg/mod/cache/download"
    escaped = lambda text: "".join("!" + char.lower() if char.isupper() else char for char in text)
    package = source / escaped(spec["module"]) / "@v" / (spec["version"] + ".zip")
    if not package.is_file() or zip_module_sum(package) != spec["moduleSum"]:
        return 0
    with zipfile.ZipFile(package) as archive:
        sums = archive.read(spec["module"] + "@" + spec["version"] + "/go.sum").decode()
        own_mod = archive.read(spec["module"] + "@" + spec["version"] + "/go.mod")
    expected = {(spec["module"], spec["version"]): spec["moduleSum"]}
    for row in sums.splitlines():
        name, version, value = row.split()
        expected[name, version] = value
    destination = build_home / "pkg/mod/cache/download"
    copied = 0
    available = {}
    for (name, version), expected_sum in expected.items():
        mod_only = version.endswith("/go.mod")
        version = version.removesuffix("/go.mod")
        base = source / escaped(name) / "@v"
        archive = base / (escaped(version) + (".mod" if mod_only else ".zip"))
        if not archive.is_file():
            continue
        actual = ("h1:" + base64.b64encode(hashlib.sha256(
            (hashlib.sha256(archive.read_bytes()).hexdigest() + "  go.mod\n").encode()).digest()).decode()
            if mod_only else zip_module_sum(archive))
        if actual != expected_sum:
            raise ValueError("Cached Go source differs from authenticated gopls/go.sum: " + name)
        target = destination / escaped(name) / "@v"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, target / archive.name)
        available.setdefault(target, set()).add(version)
        if not mod_only:
            (target / (escaped(version) + ".ziphash")).write_text(actual)
            info = base / (escaped(version) + ".info")
            if info.is_file() and json.loads(info.read_text())["Version"] == version:
                shutil.copyfile(info, target / info.name)
            if name == spec["module"] and version == spec["version"]:
                (target / (escaped(version) + ".mod")).write_bytes(own_mod)
        copied += 1
    # Go consults @v/list for module deprecation metadata even when installing
    # an exact version. Expose the authenticated local cache as a file proxy.
    for target, versions in available.items():
        (target / "list").write_text("\n".join(sorted(versions)) + "\n")
    emit("authenticated-public-go-cache", entries=copied)
    return copied


def gopls_build(prepared, cache, lock):
    go = prepared / "runtimes/language-servers/go"
    target = prepared / "runtimes/language-servers/gopls"
    target.mkdir(exist_ok=True)
    build_home = cache / "gopls-build"
    build_home.mkdir(exist_ok=True)
    seeded = seed_go_modules(build_home, lock["goplsBuild"])
    env = dict(os.environ)
    env.update(GOROOT=str(go), GOPATH=str(build_home), GOMODCACHE=str(build_home / "pkg/mod"),
               GOCACHE=str(build_home / "cache"), GOTOOLCHAIN="local", CGO_ENABLED="0",
               GOBIN=str(target), GOPROXY=(build_home / "pkg/mod/cache/download").resolve().as_uri() if seeded else "https://proxy.golang.org|https://goproxy.cn",
               GOSUMDB="off" if seeded else "sum.golang.org", GOENV="off", GOPRIVATE="", GONOPROXY="", GONOSUMDB="", GOMAXPROCS="4")
    env["PATH"] = str(go / "bin") + os.pathsep + env.get("PATH", "")
    spec = lock["goplsBuild"]
    emit("gopls-build", module=spec["module"], version=spec["version"])
    output = subprocess.run([str(go / "bin/go.exe"), "install", "-trimpath", spec["module"] + "@" + spec["version"]],
                            env=env, cwd=build_home, capture_output=True, text=True, encoding="utf-8", timeout=900)
    (cache / "gopls-build.log").write_text(output.stdout + output.stderr, encoding="utf-8")
    if output.returncode:
        raise ValueError("Fixed gopls source build failed; see gopls-build.log")
    metadata = subprocess.check_output([str(go / "bin/go.exe"), "version", "-m", str(target / "gopls.exe")], env=env, text=True, encoding="utf-8")
    if f"\tmod\t{spec['module']}\t{spec['version']}\t{spec['moduleSum']}" not in metadata:
        raise ValueError("Built gopls module does not match the pinned Go sumdb identity")
    (target / "build-provenance.txt").write_text(metadata, encoding="utf-8")
    notices = target / "licenses"
    notices.mkdir()
    for folder in (build_home / "pkg/mod").rglob("*"):
        if folder.is_file() and folder.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
            relative = folder.relative_to(build_home / "pkg/mod")
            output = notices / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(folder, output)


def registry(root):
    base = "runtimes/language-servers/"
    path = lambda value: {"path": base + value}
    common = {"PATH": {"paths": [base + "go/bin"], "inherit": True},
              "GOROOT": path("go"), "GOTOOLCHAIN": "local", "CGO_ENABLED": "0",
              "GOCACHE": {"dataPath": "go/build-cache"}, "GOMODCACHE": {"dataPath": "go/modules"},
              "GOPATH": {"dataPath": "go/path"}, "GOTELEMETRY": "off"}
    std = next((root / (base + "rust/lib/rustlib/x86_64-pc-windows-msvc/lib")).glob("libstd-*.rlib"))
    return {"schemaVersion": 1, "servers": [
        {"id": "gopls", "name": "Go (gopls)", "version": "0.23.0 / Go 1.27.0", "command": base + "gopls/gopls.exe", "args": [], "env": common,
         "requiredFiles": [base + "go/bin/go.exe", base + "go/VERSION", base + "go/src/fmt/print.go"], "requiredDirectories": [base + "go/pkg/tool/windows_amd64"]},
        {"id": "rust", "name": "Rust Analyzer", "version": "1.98.1", "command": base + "rust/bin/rust-analyzer.exe", "args": [],
         "env": {"PATH": {"paths": [base + "rust/bin"], "inherit": True}, "RUST_SRC_PATH": path("rust/lib/rustlib/src/rust/library"),
                 "RUSTC": path("rust/bin/rustc.exe"), "CARGO": path("rust/bin/cargo.exe"), "CARGO_HOME": {"dataPath": "rust/cargo"},
                 "CARGO_TARGET_DIR": {"dataPath": "rust/target"}},
         "requiredFiles": [base + "rust/bin/rustc.exe", base + "rust/bin/cargo.exe", std.relative_to(root).as_posix(), base + "rust/lib/rustlib/src/rust/library/std/src/lib.rs"],
         "initializationOptions": {"cargo": {"sysroot": path("rust")}}},
        {"id": "csharp", "name": "C# Language Server", "version": "0.28.0 / .NET 10.0.401", "command": base + "dotnet/dotnet.exe", "args": [path("csharp/CSharpLanguageServer.dll")],
         "env": {"DOTNET_ROOT": path("dotnet"), "DOTNET_HOST_PATH": path("dotnet/dotnet.exe"), "DOTNET_MULTILEVEL_LOOKUP": "0",
                 "DOTNET_CLI_HOME": {"dataPath": "dotnet/cli"}, "NUGET_PACKAGES": {"dataPath": "dotnet/packages"},
                 "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "DOTNET_NOLOGO": "1",
                 "DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE": "true", "PATH": {"paths": [base + "dotnet"], "inherit": True}},
         "requiredFiles": [base + "dotnet/sdk/10.0.401/MSBuild.dll", base + "csharp/CSharpLanguageServer.runtimeconfig.json"],
         "requiredDirectories": [base + "dotnet/packs/Microsoft.NETCore.App.Ref", base + "dotnet/shared/Microsoft.NETCore.App"]}
    ]}


def prepare(cache, allow_download=True, download_only=False):
    cache = cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    with ThreadPoolExecutor(max_workers=4) as executor:
        inputs = list(executor.map(lambda item: download(item, cache, allow_download), lock["inputs"]))
    if download_only:
        return {"downloaded": True, "inputs": len(inputs)}
    final = cache / "prepared"
    receipt_path = cache / "sdk-receipt.json"
    if final.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt["lockSha256"] != digest(LOCK) or receipt["files"] != inventory(final):
            raise ValueError("Prepared SDK cache changed; use a new cache")
        return {"prepared": True, "root": str(final), "receipt": str(receipt_path), "files": len(receipt["files"])}
    prepared = cache / ("preparing-" + uuid.uuid4().hex)
    tools = prepared / "runtimes/language-servers"
    tools.mkdir(parents=True)
    for record, source in zip(lock["inputs"], inputs):
        emit("extract", id=record["id"])
        if record["id"] == "go":
            extract_zip(source, tools / "go", strip="go")
        elif record["id"] == "dotnet":
            extract_zip(source, tools / "dotnet")
        elif record["id"] == "csharp":
            extract_zip(source, tools / "csharp", prefix="tools/net10.0/any")
            # Retain original package metadata/notices without duplicating
            # tools already in their portable runtime location.
            extract_zip(source, tools / "csharp/package-metadata", exclude=("tools",))
        else:
            extract_rust(source, tools / "rust", record["component"])
    prune_official_test_fixtures(prepared)
    gopls_build(prepared, cache, lock)
    fragment = registry(prepared)
    (prepared / "registry-sdks.json").write_text(json.dumps(fragment, indent=2) + "\n", encoding="utf-8")
    receipt = {"schemaVersion": 1, "versions": lock["versions"], "lockSha256": digest(LOCK),
               "inputs": [{"file": item["file"], "sha256": digest(cache / item["file"])} for item in lock["inputs"]],
               "files": inventory(prepared), "prunedOfficialTestFixtures": list(PRUNED_TEST_FIXTURES)}
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    prepared.rename(final)
    return {"prepared": True, "root": str(final), "receipt": str(receipt_path), "files": len(receipt["files"]), "versions": lock["versions"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--no-download", action="store_true")
    parser.add_argument("--download-only", action="store_true")
    arguments = parser.parse_args()
    print(json.dumps(prepare(arguments.cache, not arguments.no_download, arguments.download_only)), flush=True)

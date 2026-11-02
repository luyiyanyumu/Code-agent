"""Prepare portable Swift and open-source PHP analysis tools from fixed inputs.

MSI administrative images only extract into the supplied build directory. No
product is installed, and no system/user environment or registration is changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

from prepare_windows_java import inventory, safe_member, sha

LOCK = Path(__file__).with_name("language-platform-inputs.json")


def download(record: dict, cache: Path) -> Path:
    target = cache / record["name"]
    if target.is_file() and sha(target) == record["sha256"]:
        return target
    for direct in (True, False):
        temporary = target.with_suffix(target.suffix + ".partial")
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if direct else urllib.request.build_opener()
            with opener.open(record["url"], timeout=60) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output, 1024 * 1024)
            if sha(temporary) != record["sha256"]:
                raise ValueError("Pinned platform input differs: " + record["name"])
            temporary.replace(target)
            return target
        except (OSError, ValueError):
            if not direct:
                raise
    raise RuntimeError("Platform download failed")


def run(args: list[str], *, timeout: int = 600) -> None:
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode not in (0, 3010):
        raise RuntimeError("Platform extraction failed: " + args[0] + "\n" + result.stdout.decode(errors="replace")[-3000:])


def swift_inputs(cache: Path, lock: dict) -> Path:
    directory = cache / "swift-inputs"
    directory.mkdir(exist_ok=True)
    def valid(row):
        path = directory / row["name"]
        return path.is_file() and hashlib.file_digest(path.open("rb"), "sha512").hexdigest() == row["sha512"]
    if all(valid(row) for row in lock["payloads"]):
        return directory
    original = download({**lock["installer"], "name": "swift-6.4.0-RELEASE-windows10.exe"}, cache)
    # Burn's payload CAB has an authenticated size/hash in the pinned signed
    # bootstrapper. Locate its normal CAB header, without a WiX dependency.
    size = lock["container"]["size"]
    with original.open("rb") as stream:
        head = stream.read(8 * 1024 * 1024)
    offset = next((position for position in range(len(head) - 12)
                   if head[position:position+4] == b"MSCF" and struct.unpack_from("<I", head, position+8)[0] == size), None)
    if offset is None:
        raise ValueError("Cannot locate authenticated Swift payload container")
    cabinet = cache / "swift-payload.cab"
    digest = hashlib.sha512()
    with original.open("rb") as stream, cabinet.open("wb") as output:
        stream.seek(offset)
        remaining = size
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("Truncated Swift container")
            output.write(chunk); digest.update(chunk); remaining -= len(chunk)
    if digest.hexdigest() != lock["container"]["sha512"]:
        raise ValueError("Swift container signature digest differs")
    # Burn's checked original contains flat a0..aN names, never file paths.
    with cabinet.open("rb") as stream:
        header = stream.read(36)
        stream.seek(struct.unpack_from("<I", header, 16)[0])
        for _ in range(struct.unpack_from("<H", header, 28)[0]):
            stream.read(16)
            name = bytearray()
            while (byte := stream.read(1)) not in (b"\0", b""):
                name.extend(byte)
                if len(name) > 128:
                    raise ValueError("Invalid Swift CAB name")
            if not re.fullmatch(rb"a\d+", name):
                raise ValueError("Swift container contains a non-flat file name")
    extracted = Path(tempfile.mkdtemp(prefix="swift-cab-", dir=cache))
    run([str(Path(os.environ["SystemRoot"]) / "System32/expand.exe"), "-F:*", str(cabinet), str(extracted)])
    for row in lock["payloads"]:
        shutil.copyfile(extracted / row["containerName"], directory / row["name"])
        if not valid(row):
            raise ValueError("Swift package differs from signed bootstrapper: " + row["name"])
    return directory


def decoded_msi_name(name: str) -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz._"
    result = ""
    for character in name:
        value = ord(character)
        if 0x3800 <= value < 0x4800:
            value -= 0x3800
            result += alphabet[value & 63] + alphabet[value >> 6]
        elif 0x4800 <= value < 0x4840:
            result += alphabet[value - 0x4800]
        else:
            result += character
    return result


def msi_file_names(package: Path) -> dict[str, str]:
    """Read merge-module cabinet IDs through the read-only MSI database API."""
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("msi")
    handle = wintypes.UINT
    api.MsiOpenDatabaseW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(handle))
    api.MsiDatabaseOpenViewW.argtypes = (handle, wintypes.LPCWSTR, ctypes.POINTER(handle))
    api.MsiViewExecute.argtypes = (handle, handle)
    api.MsiViewFetch.argtypes = (handle, ctypes.POINTER(handle))
    api.MsiRecordGetStringW.argtypes = (handle, wintypes.UINT, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    database, view = handle(), handle()
    if api.MsiOpenDatabaseW(str(package), None, ctypes.byref(database)):
        raise ValueError("Cannot read Swift runtime merge module")
    def string(row, field):
        length = wintypes.DWORD()
        status = api.MsiRecordGetStringW(row, field, None, ctypes.byref(length))
        if status not in (0, 234): raise ValueError("Invalid MSI file name")
        buffer = ctypes.create_unicode_buffer(length.value + 1)
        length.value += 1
        if api.MsiRecordGetStringW(row, field, buffer, ctypes.byref(length)):
            raise ValueError("Cannot decode MSI file name")
        return buffer.value
    result = {}
    try:
        if api.MsiDatabaseOpenViewW(database, "SELECT `File`, `FileName` FROM `File`", ctypes.byref(view)) or api.MsiViewExecute(view, 0):
            raise ValueError("Swift runtime file table is missing")
        while True:
            row = handle()
            status = api.MsiViewFetch(view, ctypes.byref(row))
            if status == 259: break
            if status: raise ValueError("Cannot read Swift runtime file table")
            try:
                key, name = string(row, 1), string(row, 2).split("|")[-1]
                if Path(name).name != name or any(char in name for char in "/\\:"):
                    raise ValueError("Invalid Swift runtime file name")
                result[key] = name
            finally: api.MsiCloseHandle(row)
    finally:
        if view: api.MsiCloseHandle(view)
        api.MsiCloseHandle(database)
    return result


def swift_runtime_modules(swift: Path, scratch: Path) -> None:
    try:
        import olefile
    except ImportError as error:
        raise RuntimeError("Build dependency olefile==0.47 is required to extract Swift's original merge module") from error
    runtime = swift / "runtime"
    runtime.mkdir(exist_ok=True)
    modules = list(swift.rglob("rtl.shared.amd64.msm"))
    if len(modules) != 1:
        raise ValueError("Swift x64 runtime merge module is missing or ambiguous")
    with olefile.OleFileIO(modules[0]) as package:
        streams = [name for name in package.listdir() if decoded_msi_name(name[-1]) == "MergeModule.CABinet"]
        if len(streams) != 1:
            raise ValueError("Swift runtime cabinet is missing")
        cabinet = scratch / "swift-runtime.cab"
        cabinet.write_bytes(package.openstream(streams[0]).read())
    extracted = Path(tempfile.mkdtemp(prefix="runtime-files-", dir=scratch))
    run([str(Path(os.environ["SystemRoot"]) / "System32/expand.exe"), "-F:*", str(cabinet), str(extracted)])
    names = msi_file_names(modules[0])
    for path in extracted.rglob("*"):
        if path.is_file():
            name = names.get(path.name)
            if name and name.lower().endswith((".dll", ".exe")):
                shutil.copyfile(path, runtime / name)
    if not (runtime / "swiftCore.dll").is_file():
        raise ValueError("Swift runtime extraction did not produce swiftCore.dll")


def registry() -> dict:
    from prepare_language_phpantom import registry_entry
    base = "runtimes/language-servers"
    swift = base + "/swift"
    binary = swift + "/Toolchains/6.4.0+Asserts/usr/bin"
    sdk = swift + "/Platforms/6.4.0/Windows.platform/Developer/SDKs/Windows.sdk"
    return {"schemaVersion": 1, "servers": [
        registry_entry(),
        {"id": "swift", "name": "SourceKit-LSP (Swift)", "version": "6.4.0", "command": binary + "/sourcekit-lsp.exe", "args": [],
         "env": {"SDKROOT": {"path": sdk}, "PATH": {"paths": [binary, swift + "/runtime", "runtimes/webview2"], "inherit": True},
                 "XDG_CACHE_HOME": {"dataPath": "swift/cache"}, "XDG_CONFIG_HOME": {"dataPath": "swift/config"}},
         "initializationOptions": {"fallbackBuildSystem": {"sdk": {"path": sdk},
             "swiftCompilerFlags": ["-module-cache-path", {"dataPath": "swift/module-cache"}]}},
         "requiredFiles": [binary + "/sourcekitdInProc.dll", swift + "/runtime/swiftCore.dll"],
         "requiredDirectories": [sdk]}
    ]}


def prepare(cache: Path, output: Path) -> dict:
    cache = cache.resolve(); output = output.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    receipt_path = output.with_suffix(".inventory.json")
    lock = json.loads(LOCK.read_text())
    if output.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt.get("lockSha256") != sha(LOCK) or inventory(output) != receipt["files"]:
            raise ValueError("Prepared platform tools changed; use a fresh output")
        return {"prepared": True, "output": str(output), "receipt": str(receipt_path), "files": len(receipt["files"])}
    scratch = Path(tempfile.mkdtemp(prefix="platform-extract-", dir=cache))
    tools = scratch / "result"; (tools / "runtimes/language-servers/php").mkdir(parents=True)
    php = download(lock["php"], cache)
    with zipfile.ZipFile(php) as archive:
        for entry in archive.infolist():
            name = safe_member(entry.filename)
            target = tools / "runtimes/language-servers/php" / Path(*name.parts)
            if entry.is_dir(): target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open("wb") as destination: shutil.copyfileobj(source, destination)
    from prepare_language_phpantom import prepare as prepare_phpantom
    prepare_phpantom(cache, tools)
    packages = swift_inputs(cache, lock["swift"])
    swift = tools / "runtimes/language-servers/swift"
    # Windows Installer itself is not long-path aware. Build its admin image
    # under a short workspace path, then copy the actual Swift prefix.
    short_parent = Path(__file__).resolve().parents[1] / ".tmp"
    short_parent.mkdir(exist_ok=True)
    admin = Path(tempfile.mkdtemp(prefix="sw-", dir=short_parent)).resolve()
    if not admin.is_relative_to(short_parent.resolve()):
        raise ValueError("Swift administrative image is outside the build workspace")
    for name in ("bld.asserts", "cli.asserts", "ide.asserts", "rtl.amd64", "res", "windows"):
        print(json.dumps({"extracting": "Swift " + name}), flush=True)
        run([str(Path(os.environ["SystemRoot"]) / "System32/msiexec.exe"), "/a", str(packages / (name + ".msi")),
             "/qn", "TARGETDIR=" + str(admin), "/L*v", str(scratch / (name + ".log"))])
    prefix = admin / "LocalApp/Programs/Swift"
    if not prefix.is_dir():
        raise ValueError("Official Swift administrative image has an unexpected prefix")
    shutil.copytree(prefix, swift)
    swift_runtime_modules(swift, scratch)
    fragment = registry()
    (tools / "registry-platforms.json").write_text(json.dumps(fragment, indent=2) + "\n")
    receipt = {"schemaVersion": 1, "lockSha256": sha(LOCK), "files": inventory(tools)}
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    tools.rename(output)
    return {"prepared": True, "output": str(output), "receipt": str(receipt_path), "files": len(receipt["files"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.cache, args.output)), flush=True)

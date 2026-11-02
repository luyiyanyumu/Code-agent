"""Prepare pinned Windows Java tools without a system install or user settings.

Run via prepare-windows-java.ps1. Only verified original archives are extracted;
the reusable extracted cache and resumed payload are checked against a receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
import zipfile

INPUTS = (
    ("OpenJDK21U-jdk_x64_windows_hotspot_21.0.12.1_1.zip", "f9d6e191ab098c0d416e7d588a24420a8621cd2f4720dab2459b8b7b2d2d8b4e"),
    ("jdt-language-server-1.61.0-202609031315.tar.gz", "338e7e73d61836651ba2453919a0d34fa763eb4e7c03342092309bffb8934c64"),
    ("lombok-1.18.48.jar", "85477a4655ebb2c074a9099cfb749be454449fee564d4282610df1b85f7c508b"),
)
VERSIONS = {"java": "21.0.12.1+1", "jdtls": "1.61.0-202609031315", "lombok": "1.18.48"}
PATHS = {"java": "runtimes/java/bin/java.exe", "jdtls": "runtimes/jdtls", "lombok": "runtimes/lombok/lombok.jar"}


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_member(name: str) -> PurePosixPath:
    value = PurePosixPath(name.replace("\\", "/"))
    if value.is_absolute() or ".." in value.parts or ":" in name or not value.parts:
        raise ValueError("Unsafe Java archive member")
    return value


def inventory(directory: Path) -> dict[str, str]:
    if directory.is_symlink() or directory.is_junction():
        raise ValueError("Java tools cannot include filesystem links")
    files = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Java tools cannot include filesystem links")
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = sha(path)
    return files


def prepare(cache: Path, payload: Path | None = None) -> dict:
    cache = cache.resolve()
    for name, digest in INPUTS:
        if sha(cache / name) != digest:
            raise ValueError("Pinned Java input checksum differs: " + name)
    tools = cache / "java-tools-21.0.12.1-jdt1.61.0-lombok1.18.48"
    receipt_path = cache / (tools.name + ".json")
    if tools.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("inputs") != dict(INPUTS) or inventory(tools) != receipt.get("files"):
            raise ValueError("Prepared Java cache changed; use a new cache directory")
    else:
        final_tools = tools
        tools = Path(tempfile.mkdtemp(prefix="java-tools-extract-", dir=cache))
        (tools / "java").mkdir()
        with zipfile.ZipFile(cache / INPUTS[0][0]) as archive:
            for entry in archive.infolist():
                name = safe_member(entry.filename)
                if name.parts[0] != "jdk-21.0.12.1+1" or len(name.parts) == 1:
                    if len(name.parts) == 1 and entry.is_dir():
                        continue
                    raise ValueError("Unexpected JDK archive root")
                target = tools / "java" / Path(*name.parts[1:])
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
        (tools / "jdtls").mkdir()
        with tarfile.open(cache / INPUTS[1][0], "r:gz") as archive:
            for entry in archive:
                if entry.isdir() and entry.name in {".", "./"}:
                    continue
                name = safe_member(entry.name)
                if not (entry.isfile() or entry.isdir()):
                    raise ValueError("JDT archive must contain regular files/directories only")
                target = tools / "jdtls" / Path(*name.parts)
                if entry.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(entry) as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
        (tools / "lombok").mkdir()
        shutil.copyfile(cache / INPUTS[2][0], tools / "lombok/lombok.jar")
        # Include Lombok's original license as a visible file as well as its JAR.
        with zipfile.ZipFile(cache / INPUTS[2][0]) as archive:
            for name in ("LICENSE", "LICENSE.txt"):
                if name in archive.namelist():
                    (tools / "lombok/LICENSE.txt").write_bytes(archive.read(name))
                    break
        for name in ("java/bin/java.exe", "java/bin/javac.exe", "java/NOTICE", "jdtls/bin/jdtls", "jdtls/bin/jdtls.py", "jdtls/config_win/config.ini", "lombok/lombok.jar"):
            if not (tools / name).is_file():
                raise ValueError("Prepared Java entry missing: " + name)
        if not list((tools / "jdtls/plugins").glob("org.eclipse.equinox.launcher_*.jar")) or not (tools / "java/legal").is_dir():
            raise ValueError("Prepared Java libraries or license notices missing")
        receipt = {"inputs": dict(INPUTS), "versions": VERSIONS, "files": inventory(tools)}
        if not tools.is_relative_to(cache) or not final_tools.is_relative_to(cache) or final_tools.exists():
            raise ValueError("Prepared Java cache target changed")
        tools.rename(final_tools)
        tools = final_tools
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    if payload is not None:
        payload = payload.resolve()
        manifest_path = payload / "desktop-runtime.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if manifest.get("schemaVersion") != 1 or not (payload / "package.json").is_file():
            raise ValueError("Payload must be a prepared application directory")
        runtimes = payload / "runtimes"
        if runtimes.is_symlink() or runtimes.is_junction() or not runtimes.resolve().is_relative_to(payload):
            raise ValueError("Payload runtime directory escaped the application")
        runtimes.mkdir(exist_ok=True)
        for name in VERSIONS:
            target = runtimes / name
            expected = {key[len(name)+1:]: digest for key, digest in receipt["files"].items() if key.startswith(name + "/")}
            if target.exists():
                if inventory(target) != expected:
                    raise ValueError("Existing payload Java tools differ; use a fresh payload: " + name)
            else:
                shutil.copytree(tools / name, target)
        manifest.update(PATHS)
        manifest.setdefault("runtimeVersions", {}).update(VERSIONS)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        (runtimes / "java-tools.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return {"prepared": True, "tools": str(tools), "receipt": str(receipt_path), "files": len(receipt["files"]), "versions": VERSIONS, "payload": str(payload) if payload else None}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--payload", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(prepare(arguments.cache, arguments.payload)))

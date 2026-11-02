"""Prepare the MIT native Windows PHP language server from a fixed release.

Phpactor requires POSIX and is not supported on Windows. PHPantom embeds its
PHP parser and standard library types, so analysis needs no global PHP/Composer.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import tarfile
import zipfile

from prepare_language_platforms import download
from prepare_windows_java import safe_member, sha
from prepare_language_node import inventory

LOCK = Path(__file__).with_name("language-phpantom-inputs.json")


def registry_entry() -> dict:
    base = "runtimes/language-servers/phpantom"
    return {"id": "php", "name": "PHPantom (PHP)", "version": "0.11.1",
            "command": "runtimes/python/python.exe", "args": [{"path": base + "/launch.py"}],
            "requiredFiles": [base + "/launch.py", base + "/phpantom_lsp.exe", base + "/LICENSE",
                              "runtimes/language-servers/php/php.exe", "runtimes/language-servers/php/php8.dll",
                              "runtimes/webview2/vcruntime140.dll", "runtimes/webview2/vcruntime140_1.dll", "runtimes/webview2/msvcp140.dll"],
            "env": {"XDG_CACHE_HOME": {"dataPath": "phpantom/cache"},
                    "XDG_CONFIG_HOME": {"dataPath": "phpantom/config"}, "PYTHONDONTWRITEBYTECODE": "1",
                    "PATH": {"paths": ["runtimes/language-servers/php", "runtimes/webview2"], "inherit": True}}}


def prepare_notices(cache: Path, directory: Path, lock: dict) -> None:
    for record in lock.get("notices", []):
        source = download(record, cache)
        target = directory / "licenses" / record["name"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    def crate(record: dict) -> None:
        if not record["url"].startswith("https://static.crates.io/crates/"):
            raise ValueError("Unexpected Rust crate license source")
        archive = download(record, cache / "crate-cache")
        with tarfile.open(archive, "r:gz") as source:
            for entry in record["licenses"]:
                member_name = safe_member(entry["member"])
                relative = safe_member(entry["path"])
                member = source.getmember(member_name.as_posix())
                if not member.isfile():
                    raise ValueError("Expected an ordinary crate license file")
                target = directory / "licenses/third-party" / Path(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as data, target.open("wb") as output:
                    shutil.copyfileobj(data, output)
                if sha(target) != entry["sha256"]:
                    raise ValueError("Crate license hash differs")

    (cache / "crate-cache").mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(crate, lock.get("crateNotices", [])))


def prepare(cache: Path, output: Path) -> dict:
    cache, output = cache.resolve(), output.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    directory = output / "runtimes/language-servers/phpantom"
    if directory.exists():
        raise ValueError("PHPantom output already exists; verify its receipt before reuse")
    directory.mkdir(parents=True)
    archive_path = download(lock["archive"], cache)
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            name = safe_member(member.filename)
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("PHPantom archive contains a link")
            target = directory.joinpath(*name.parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
    license_path = download(lock["LICENSE"], cache)
    shutil.copyfile(license_path, directory / "LICENSE")
    prepare_notices(cache, directory, lock)
    shutil.copyfile(LOCK, directory / "inputs.json")
    launcher = Path(__file__).with_name("language-php-launch.py")
    shutil.copyfile(launcher, directory / "launch.py")
    if not (directory / "phpantom_lsp.exe").is_file():
        raise ValueError("Official PHPantom archive has an unexpected executable layout")
    receipt = {"schemaVersion": 1, "lockSha256": sha(LOCK), "launcherSha256": sha(launcher),
               "files": inventory(directory), "server": registry_entry()}
    (output / "phpantom-inventory.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.cache, args.output)
    print(json.dumps({"prepared": True, "files": len(result["files"]), "server": result["server"]}), flush=True)

"""Build the pinned upstream SQL LSP with Node 24's SQLite, not sqlite3 addons.

The npm release still contains the old native SQLite/protocol stack. This recipe
uses an immutable upstream commit, verifies each source blob and a full npm lock,
then follows upstream's esbuild CLI recipe. No database or user config is used.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import httpx

from prepare_language_node import inventory, sha

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/language-tools-sql"


async def sources(cache: Path, lock: dict) -> Path:
    directory = cache / "pinned-source"
    directory.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(8)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        async def one(row):
            relative = PurePosixPath(row["path"])
            if relative.is_absolute() or ".." in relative.parts or ":" in row["path"]:
                raise ValueError("Unsafe upstream SQL source path")
            path = directory.joinpath(*relative.parts)
            if path.is_file() and sha(path) == row["sha256"]:
                return
            async with semaphore:
                response = await client.get(f'https://raw.githubusercontent.com/joe-re/sql-language-server/{lock["commit"]}/{row["path"]}')
                response.raise_for_status()
                value = response.content
                if hashlib.sha256(value).hexdigest() != row["sha256"] or hashlib.sha1(b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest() != row["gitSha1"]:
                    raise ValueError("Pinned upstream SQL source digest differs")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
        await asyncio.gather(*(one(row) for row in lock["files"]))
    return directory


def run(argv, *, env=None):
    result = subprocess.run([str(value) for value in argv], env=env, check=False)
    if result.returncode:
        raise RuntimeError("Pinned SQL language service preparation failed")


def prepare(cache: Path, node: Path, payload: Path | None):
    cache = cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    lock = json.loads((SOURCE / "source-lock.json").read_text(encoding="utf-8"))
    npm_lock = json.loads((SOURCE / "package-lock.json").read_text(encoding="utf-8"))
    for name, package in npm_lock["packages"].items():
        if name and (package.get("link") or not package.get("integrity") or not str(package.get("resolved", "")).startswith("https://registry.npmjs.org/")):
            raise ValueError("SQL dependencies must use integrity-pinned official npm tarballs")
    inputs = {name: sha(SOURCE / name) for name in ("source-lock.json", "package.json", "package-lock.json")}
    identity = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]
    tools = cache / ("sql-tools-" + identity)
    receipt_path = tools.with_suffix(".json")
    if tools.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt["inputs"] != inputs or inventory(tools) != receipt["files"]:
            raise ValueError("Prepared SQL tools changed; use a fresh cache directory")
    else:
        source = asyncio.run(sources(cache, lock))
        build = cache / ("build-" + identity)
        build.mkdir(exist_ok=True)
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(SOURCE / name, build / name)
        npm_cli = node.resolve().parent / "node_modules/npm/bin/npm-cli.js"
        npm_config = cache / "public-registry.npmrc"
        npm_config.write_text("registry=https://registry.npmjs.org/\naudit=false\nfund=false\n", encoding="utf-8")
        run([node, npm_cli, "ci", "--prefix", build, "--ignore-scripts", "--omit=dev", "--no-audit", "--no-fund",
             "--registry=https://registry.npmjs.org", "--userconfig", npm_config, "--cache", cache / "npm-cache"])
        final_tools = tools
        tools = Path(tempfile.mkdtemp(prefix="sql-extract-", dir=cache))
        env = {**os.environ, "NODE_PATH": str(build / "node_modules")}
        run([node, build / "node_modules/esbuild/bin/esbuild", source / "packages/server/bin/cli.ts", "--bundle", "--platform=node", "--format=cjs", "--target=node24",
             "--external:pg-native", "--external:cpu-features", "--external:*.node",
             "--alias:@joe-re/sql-parser=" + str(source / "packages/sql-parser/src/index.ts"),
             "--alias:sqlint=" + str(source / "packages/sqlint/src/index.ts"), "--outfile=" + str(tools / "sql-language-server.cjs")], env=env)
        for name in ("package-lock.json", "source-lock.json"):
            shutil.copyfile(SOURCE / name, tools / name)
        # Keep the upstream sources and all dependency license/notice files.
        for row in lock["files"]:
            path = tools / "upstream-source" / row["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / row["path"], path)
        for directory, dirs, files in os.walk(build / "node_modules"):
            dirs[:] = [name for name in dirs if name != ".bin"]
            for name in files:
                if not name.lower().startswith(("license", "licence", "copying", "notice")):
                    continue
                path = Path(directory) / name
                target = tools / "licenses" / path.relative_to(build / "node_modules")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        fragment = {"schemaVersion": 1, "servers": [{"id": "sql", "version": "1.7.0+" + lock["commit"][:12],
            "command": "runtimes/node/node.exe", "args": [{"path": "runtimes/language-servers/sql/sql-language-server.cjs"}, "up", "--method", "stdio"],
            "requiredFiles": ["runtimes/language-servers/sql/sql-language-server.cjs"],
            "initializationOptions": {"settings": {"sqlLanguageServer": {"connections": [], "lint": {}}}}}]}
        (tools / "servers.json").write_text(json.dumps(fragment, indent=2) + "\n", encoding="utf-8")
        receipt = {"schemaVersion": 1, "inputs": inputs, "commit": lock["commit"], "files": inventory(tools)}
        if not tools.is_relative_to(cache) or not final_tools.is_relative_to(cache) or final_tools.exists():
            raise ValueError("SQL preparation target changed")
        tools.rename(final_tools)
        tools = final_tools
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    if payload is not None:
        payload = payload.resolve()
        if not (payload / "desktop-runtime.json").is_file():
            raise ValueError("SQL tools require a prepared desktop payload")
        parent = payload / "runtimes/language-servers"
        parent.mkdir(exist_ok=True)
        target = parent / "sql"
        if target.exists():
            if inventory(target) != receipt["files"]:
                raise ValueError("Existing payload SQL tools differ")
        else:
            shutil.copytree(tools, target)
        shutil.copyfile(tools / "servers.json", parent / "sql-servers.json")
        shutil.copyfile(receipt_path, parent / "sql-inventory.json")
    return {"prepared": True, "tools": str(tools), "receipt": str(receipt_path), "registry": str(tools / "servers.json"),
            "files": len(receipt["files"]), "payload": str(payload) if payload else None}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--payload", type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.cache, args.node, args.payload)), flush=True)

"""Prepare redistributable Node/Python language services from immutable locks.

Build-time only. This never installs global npm packages or writes the embedded
application Python environment. Payload launch entries use absolute argv paths
resolved from the runtime registry, with no npm/pip work at application startup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
NODE = "runtimes/language-servers/node/node_modules"
PYTHON = "runtimes/language-servers/python"


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(root: Path) -> dict[str, str]:
    files = {}
    if root.is_symlink() or root.is_junction():
        raise ValueError("Language tools cannot contain links")
    def walk(directory: Path):
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink() or entry.is_junction():
                    raise ValueError("Language tools cannot contain links")
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    walk(path)
                elif entry.is_file(follow_symlinks=False):
                    files[path.relative_to(root).as_posix()] = sha(path)
    walk(root)
    return dict(sorted(files.items()))


def node_registry() -> list[dict]:
    def entry(sid, version, script, args=("--stdio",), **extra):
        return {"id": sid, "version": version, "command": "runtimes/node/node.exe",
                "args": [{"path": f"{NODE}/{script}"}, *args],
                "requiredFiles": [f"{NODE}/{script}"], **extra}
    ts_lib = {"path": f"{NODE}/typescript/lib"}
    entries = [
        entry("pyright", "1.1.414", "pyright/langserver.index.js"),
        {"id": "pylsp", "version": "1.15.0", "command": "runtimes/python/python.exe",
         "args": [{"path": f"{PYTHON}/launch.py"}, "pylsp"],
         "requiredFiles": [f"{PYTHON}/launch.py", f"{PYTHON}/site-packages/pylsp/__main__.py"],
         "env": {"PYTHONDONTWRITEBYTECODE": "1", "CODE_AGENT_LSP_CACHE": {"dataPath": "pylsp/cache"}}},
        {"id": "ruff", "version": "0.16.8", "command": f"{PYTHON}/ruff.exe", "args": ["server"],
         "requiredFiles": [f"{PYTHON}/ruff.exe"]},
        entry("typescript", "4.3.4", "typescript-language-server/lib/cli.mjs",
              requiredDirectories=[f"{NODE}/typescript/lib"],
              initializationOptions={"tsserver": {"path": {"path": f"{NODE}/typescript/lib/tsserver.js"}}}),
        # Volar 2's documented standalone mode owns TypeScript semantic work.
        # Volar 3 would require a separate tsserver-plugin request bridge.
        entry("vue", "2.2.12", "@vue/language-server/bin/vue-language-server.js",
              requiredDirectories=[f"{NODE}/typescript/lib"],
              initializationOptions={"typescript": {"tsdk": ts_lib}, "vue": {"hybridMode": False}}),
    ]
    for sid in ("json", "css", "html", "eslint"):
        extra = {}
        if sid == "json":
            extra = {"initializationOptions": {"settings": {"json": {"validate": {"enable": True}, "schemaDownload": {"enable": False}}}}}
        if sid == "eslint":
            extra = {"requiredFiles": [f"{NODE}/vscode-langservers-extracted/bin/vscode-eslint-language-server", f"{NODE}/eslint/lib/api.js"],
                     "initializationOptions": {"settings": {"eslint": {
                         "nodePath": {"path": NODE}, "validate": "on", "run": "onType", "packageManager": "npm", "useFlatConfig": True,
                         "experimental": {"useFlatConfig": False}, "problems": {"shortenToSingleLine": False}, "rulesCustomizations": [],
                         "codeActionOnSave": {"enable": False, "mode": "all"}, "options": {}, "quiet": False,
                         "codeAction": {"disableRuleComment": {"enable": True}, "showDocumentation": {"enable": True}}}}}}
        entries.append(entry(sid, "4.10.0", f"vscode-langservers-extracted/bin/vscode-{sid}-language-server", **extra))
    entries.extend([
        entry("yaml", "1.24.0", "yaml-language-server/bin/yaml-language-server"),
        entry("bash", "5.8.1", "bash-language-server/out/cli.js", ("start",)),
        entry("dockerfile", "0.15.0", "dockerfile-language-server-nodejs/bin/docker-langserver"),
    ])
    return entries


def run(args, *, cwd=None):
    print("Preparing: " + Path(str(args[0])).name + " " + " ".join(str(a) for a in args[1:4]), flush=True)
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, check=False)
    if result.returncode:
        raise RuntimeError("Language dependency preparation failed")


def prepare(cache: Path, node: Path, python: Path, payload: Path | None = None):
    cache = cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    package_dir = ROOT / "scripts/language-tools-node"
    requirements = ROOT / "scripts/language-tools-python.lock"
    launcher = ROOT / "scripts/language-python-launch.py"
    lock = json.loads((package_dir / "package-lock.json").read_text(encoding="utf-8"))
    for name, package in lock["packages"].items():
        if not name:
            continue
        if package.get("link") or not package.get("integrity") or not str(package.get("resolved", "")).startswith("https://registry.npmjs.org/"):
            raise ValueError("Node lock must contain only integrity-pinned official registry packages")
    inputs = {"package": sha(package_dir / "package.json"), "npmLock": sha(package_dir / "package-lock.json"),
              "pythonLock": sha(requirements), "pythonLauncher": sha(launcher)}
    identity = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]
    tools = cache / ("node-python-tools-" + identity)
    receipt_file = cache / (tools.name + ".json")
    if tools.exists():
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
        if receipt.get("inputs") != inputs or receipt.get("servers") != node_registry() or inventory(tools) != receipt.get("files"):
            raise ValueError("Prepared language cache changed; use a new cache directory")
    else:
        final_tools = tools
        tools = Path(tempfile.mkdtemp(prefix="node-python-extract-", dir=cache))
        node_target = tools / "node"
        node_target.mkdir()
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(package_dir / name, node_target / name)
        npm_cli = node.resolve().parent / "node_modules/npm/bin/npm-cli.js"
        if not npm_cli.is_file():
            raise ValueError("Bundled Node/npm is required for build-time language preparation")
        npm_config = cache / "public-registry.npmrc"
        npm_config.write_text("registry=https://registry.npmjs.org/\naudit=false\nfund=false\n", encoding="utf-8")
        run([node, npm_cli, "ci", "--prefix", node_target, "--ignore-scripts", "--omit=dev", "--no-audit", "--no-fund",
             "--userconfig", npm_config, "--registry=https://registry.npmjs.org", "--cache", cache / "npm-cache"])
        python_target = tools / "python"
        site = python_target / "site-packages"
        site.mkdir(parents=True)
        wheels = cache / "wheels"
        wheels.mkdir(exist_ok=True)
        required_wheels = set(re.findall(r"--hash=sha256:([a-f0-9]{64})", requirements.read_text(encoding="utf-8")))
        if not required_wheels or not required_wheels.issubset({sha(path) for path in wheels.glob("*.whl")}):
            run([python, "-m", "pip", "--disable-pip-version-check", "download", "--only-binary=:all:",
                 "--platform", "win_amd64", "--python-version", "3.13", "--implementation", "cp", "--abi", "cp313",
                 "--require-hashes", "-r", requirements, "--dest", wheels])
        run([python, "-m", "pip", "--disable-pip-version-check", "install", "--no-index", "--find-links", wheels,
             "--require-hashes", "--no-compile", "--target", site, "-r", requirements])
        ruff = next((path for directory in ("Scripts", "bin") for path in (site / directory).glob("ruff.exe")), None)
        if ruff is None:
            raise ValueError("Ruff wheel did not provide the pinned standalone Windows binary")
        shutil.copyfile(ruff, python_target / "ruff.exe")
        # Console wrappers contain build-machine prefixes. Our bootstrap/native
        # entry points replace them; these exact fresh-cache directories are safe.
        for directory in (site / "Scripts", site / "bin"):
            if directory.exists():
                if not directory.resolve().is_relative_to(tools.resolve()) or directory.is_symlink() or directory.is_junction():
                    raise ValueError("Unexpected Python wrapper directory")
                shutil.rmtree(directory)
        shutil.copyfile(launcher, python_target / "launch.py")
        registry = {"schemaVersion": 1, "servers": node_registry()}
        (tools / "servers.json").write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for server in registry["servers"]:
            for name in server.get("requiredFiles", []):
                relative = name.removeprefix("runtimes/language-servers/")
                if not (tools / relative).is_file():
                    raise ValueError("Prepared language entry missing: " + name)
        receipt = {"schemaVersion": 1, "inputs": inputs, "servers": registry["servers"], "files": inventory(tools)}
        if not tools.is_relative_to(cache) or not final_tools.is_relative_to(cache) or final_tools.exists():
            raise ValueError("Prepared language cache target changed")
        tools.rename(final_tools)
        tools = final_tools
        receipt_file.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if payload is not None:
        payload = payload.resolve()
        if not (payload / "desktop-runtime.json").is_file():
            raise ValueError("Language tools need a prepared desktop payload")
        parent = payload / "runtimes/language-servers"
        parent.mkdir(exist_ok=True)
        for name in ("node", "python"):
            target = parent / name
            expected = {key[len(name)+1:]: digest for key, digest in receipt["files"].items() if key.startswith(name + "/")}
            if target.exists():
                if inventory(target) != expected:
                    raise ValueError("Existing payload language tools differ: " + name)
            else:
                shutil.copytree(tools / name, target)
        shutil.copyfile(tools / "servers.json", parent / "node-python-servers.json")
        shutil.copyfile(receipt_file, parent / "node-python-inventory.json")
    return {"prepared": True, "tools": str(tools), "receipt": str(receipt_file), "registry": str(tools / "servers.json"),
            "files": len(receipt["files"]), "servers": [row["id"] for row in receipt["servers"]], "payload": str(payload) if payload else None}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(os.sys.executable))
    parser.add_argument("--payload", type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.cache, args.node, args.python, args.payload), ensure_ascii=False), flush=True)

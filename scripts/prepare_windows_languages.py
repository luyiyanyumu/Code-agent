"""Prepare and merge all non-Java Windows language services at build time.

Prepared groups can be reused with --group-root NAME=PATH. --merge-existing
never downloads or repeats group inventory checks. Only each group's explicit
runtime directories are copied; the final registry is checked by both runtimes.

Fresh build-machine prerequisites: Windows x64, Python/pip, Node/npm, and MSYS2
UCRT64 GCC/make for compiling Ruby gems (--ruby-build-tools). MSYS2 is never
installed automatically or shipped. Go/.NET/Rust and all user-facing language
dependencies come from pinned runtime inputs. olefile 0.47 is installed only
into the isolated build-deps directory. Reusing prepared groups needs no MSYS2.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GROUPS = {
    "node": (("node", "python"), "node-python-servers.json"),
    "sql": (("sql",), "sql-servers.json"),
    "native": (("clangd", "dart", "kotlin", "lua", "markdown", "ruby", "terraform", "zig", "zls", "toml", "shell"), "native-servers.json"),
    "sdks": (("go", "gopls", "rust", "dotnet", "csharp"), "sdk-servers.json"),
    "platforms": (("php", "phpantom", "swift"), "platform-servers.json"),
}
FRAGMENTS = {
    "node": ("node-python-servers.json", "servers.json"),
    "sql": ("sql-servers.json", "servers.json"),
    "native": ("native-servers.json", "native-fragment.json"),
    "sdks": ("sdk-servers.json", "registry-sdks.json"),
    "platforms": ("platform-servers.json", "registry-platforms.json"),
}
REGISTRY = "runtimes/language-tools.json"


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run(command, *, env=None) -> dict:
    command = [str(value) for value in command]
    print(json.dumps({"stage": "prepare", "command": command[:5]}), flush=True)
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    result = None
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                result = value
        except ValueError:
            pass
    if process.wait() != 0:
        raise RuntimeError("Language preparation failed: " + Path(command[0]).name)
    return result or {}


def group_paths(name: str, root: Path) -> tuple[dict[str, Path], Path]:
    root = root.resolve()
    owners, _ = GROUPS[name]
    candidates = [root / "runtimes/language-servers", root]
    for base in candidates:
        if all((base / owner).is_dir() for owner in owners):
            directories = {owner: base / owner for owner in owners}
            break
    else:
        # The SQL helper's prepared root is itself the SQL runtime directory.
        if name == "sql" and (root / "sql-language-server.cjs").is_file():
            directories = {"sql": root}
        else:
            raise ValueError(f"Prepared {name} group is missing owned directories: {root}")
    for base in (root / "runtimes/language-servers", root):
        for file in FRAGMENTS[name]:
            if (base / file).is_file():
                return directories, base / file
    raise ValueError(f"Prepared {name} group has no registry fragment: {root}")


def checked_files(root: Path) -> dict[str, Path]:
    if root.is_symlink() or root.is_junction():
        raise ValueError("Prepared runtime root must not be a link: " + str(root))
    files = {}
    def walk(folder: Path):
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.is_symlink() or entry.is_junction():
                    raise ValueError("Prepared runtimes must not contain links: " + entry.path)
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    walk(path)
                elif entry.is_file(follow_symlinks=False):
                    files[path.relative_to(root).as_posix()] = path
                else:
                    raise ValueError("Unsupported runtime entry: " + entry.path)
    walk(root)
    return files


def copy_owned(source: Path, target: Path, policy: str) -> str:
    if source.is_symlink() or source.is_junction():
        raise ValueError("Prepared runtime root must not be a link")
    source = source.resolve()
    if target.is_symlink() or target.is_junction():
        raise ValueError("Runtime destination must not be a link")
    if source == target.resolve():
        return "already-in-payload"
    if source.is_relative_to(target.resolve()) or target.resolve().is_relative_to(source):
        raise ValueError("Runtime source and destination must not overlap")
    if target.exists() and policy == "keep":
        # Explicit reuse is for a previously prepared private staging tree.
        # Critical dependencies are still checked below by Python and JS.
        if not target.is_dir():
            raise ValueError("Existing runtime destination is not a directory")
        return "kept-existing-explicitly"
    sources = checked_files(source)
    if target.exists():
        if not target.is_dir() or policy == "error":
            raise ValueError("Existing runtime directory needs --existing-policy compare or keep: " + str(target))
        targets = checked_files(target)
        if set(targets) - set(sources):
            raise ValueError("Existing runtime directory contains unexpected files: " + str(target))
        shared = sorted(set(targets) & set(sources))
        def same(relative):
            a, b = sources[relative], targets[relative]
            return a.stat().st_size == b.stat().st_size and sha(a) == sha(b)
        with ThreadPoolExecutor(max_workers=8) as pool:
            for relative, matches in zip(shared, pool.map(same, shared)):
                if not matches:
                    raise ValueError("Conflicting runtime file; existing file was preserved: " + str(target / relative))
    else:
        targets = {}
    target.mkdir(parents=True, exist_ok=True)
    for relative, path in sources.items():
        if relative in targets:
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    return "copied-and-compared" if targets else "copied"


def merge_fragments(fragments: dict[str, dict], expected: set[str]) -> dict:
    servers = {}
    for group, fragment in fragments.items():
        if not isinstance(fragment, dict) or fragment.get("schemaVersion") != 1 or not isinstance(fragment.get("servers"), list):
            raise ValueError("Invalid language registry fragment: " + group)
        for raw in fragment["servers"]:
            sid = raw.get("id") if isinstance(raw, dict) else None
            if not isinstance(sid, str) or sid in servers:
                raise ValueError("Duplicate/invalid language server ID: " + str(sid))
            # Clone so integration does not mutate the source cache fragment.
            servers[sid] = json.loads(json.dumps(raw))
    if set(servers) != expected:
        raise ValueError("Language catalog coverage differs: missing=" + str(sorted(expected - set(servers)))
                         + "; unexpected=" + str(sorted(set(servers) - expected)))
    bash = servers.get("bash")
    if bash is not None:
        binary = "runtimes/language-servers/shell/bin"
        env = bash.setdefault("env", {})
        search = env.setdefault("PATH", {"paths": [], "inherit": True})
        if not isinstance(search, dict) or not isinstance(search.get("paths"), list):
            raise ValueError("Bash PATH must use the typed bundled search-path contract")
        if binary not in search["paths"]:
            search["paths"].append(binary)
        for tool in ("shellcheck.exe", "shfmt.exe"):
            dependency = binary + "/" + tool
            if dependency not in bash.setdefault("requiredFiles", []):
                bash["requiredFiles"].append(dependency)
    return {"schemaVersion": 1, "servers": [servers[sid] for sid in sorted(servers)]}


def platform_dependencies(args) -> dict:
    dependencies = args.cache / "build-deps"
    requirement = args.cache / "platform-build-requirements.txt"
    if not (dependencies / "olefile-0.47.dist-info/METADATA").is_file():
        requirement.parent.mkdir(parents=True, exist_ok=True)
        requirement.write_text("olefile==0.47 --hash=sha256:543c7da2a7adadf21214938bb79c83ea12b473a4b6ee4ad4bf854e7715e13d1f\n", encoding="utf-8")
        run([args.python, "-m", "pip", "--disable-pip-version-check", "install", "--no-deps", "--only-binary=:all:",
             "--require-hashes", "--target", dependencies, "-r", requirement])
    env = dict(os.environ)
    env["PYTHONPATH"] = str(dependencies)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def prepare_group(name: str, args) -> Path:
    command = [args.python, "-B", "-X", "utf8", ROOT / "scripts" / ("prepare_language_" + ("node" if name == "node" else name) + ".py")]
    if args.no_download:
        raise ValueError("No-download builds require prepared groups via --group-root or an existing --resume payload: " + name)
    if name == "node":
        result = run([*command, "--cache", args.cache / "node-python", "--node", args.node, "--python", args.python])
        return Path(result["tools"])
    if name == "sql":
        result = run([*command, "--cache", args.cache / "sql", "--node", args.node])
        return Path(result["tools"])
    if name == "native":
        output = args.cache / "native-prepared"
        if args.ruby_build_tools:
            command += ["--ruby-build-tools", args.ruby_build_tools]
        run([*command, "--cache", args.cache / "native", "--output", output])
        return output
    if name == "sdks":
        run([*command, "--cache", args.cache / "sdks"])
        return args.cache / "sdks/prepared"
    output = args.cache / "platforms-prepared"
    run([*command, "--cache", args.cache / "platforms", "--output", output], env=platform_dependencies(args))
    return output


def validate(payload: Path, registry: dict, node: Path) -> dict:
    validator = ROOT / "npm/language-runtime.mjs"
    javascript = ("import {loadLanguageTools} from " + json.dumps(validator.as_uri()) + ";"
                  "const r=await loadLanguageTools(process.argv[1],{languageTools:'runtimes/language-tools.json'});"
                  "console.log(JSON.stringify({javascriptValidated:true,servers:r.servers,criticalFiles:r.criticalFiles.length}));")
    return run([node, "--input-type=module", "--eval", javascript, payload])


def prepare(args) -> dict:
    payload = args.payload.resolve()
    manifest_path = payload / "desktop-runtime.json"
    manifest = read(manifest_path)
    if manifest.get("schemaVersion") != 1:
        raise ValueError("Language preparation requires a version-1 desktop payload")
    args.cache = args.cache.resolve()
    args.cache.mkdir(parents=True, exist_ok=True)
    args.node = (args.node or payload / "runtimes/node/node.exe").resolve()
    args.python = args.python.resolve()
    overrides = {}
    for item in args.group_root:
        name, separator, value = item.partition("=")
        if not separator or name not in GROUPS or name in overrides:
            raise ValueError("Use one --group-root NAME=PATH for a known group")
        overrides[name] = Path(value).resolve()
    fragments, provenance = {}, {}
    parent = payload / "runtimes/language-servers"
    parent.mkdir(parents=True, exist_ok=True)
    previous_path = payload / "runtimes/language-build.json"
    previous = read(previous_path).get("groups", {}) if previous_path.is_file() else {}
    for name, (owners, fragment_name) in GROUPS.items():
        group_root = overrides.get(name)
        if group_root is None and (args.resume or args.merge_existing):
            try:
                group_paths(name, payload)
                group_root = payload
            except ValueError:
                if args.merge_existing:
                    raise
        group_root = group_root or prepare_group(name, args)
        sources, fragment_path = group_paths(name, group_root)
        fragment = read(fragment_path)
        fragment_sha = hashlib.sha256((json.dumps(fragment, ensure_ascii=False, indent=2) + "\n").encode()).hexdigest()
        copied = {}
        for owner in owners:
            policy = args.existing_policy
            target = parent / owner
            if (target.is_dir() and previous.get(name, {}).get("fragmentSha256") == fragment_sha
                    and previous[name].get("owners") == list(owners)):
                policy = "keep"
            copied[owner] = copy_owned(sources[owner], target, policy)
        fragments[name] = fragment
        write(parent / fragment_name, fragment)
        provenance[name] = {"fragmentSha256": sha(parent / fragment_name), "owners": list(owners)}
        print(json.dumps({"stage": "group-ready", "group": name, "directories": copied}), flush=True)
    from code_agent.codeintel.lsp_catalog import catalog
    expected = {item.id for item in catalog()} - {"jdtls"}
    registry = merge_fragments(fragments, expected)
    # Validate paths before publishing the manifest pointer. JS validates the
    # same registry immediately afterward; no whole-runtime re-inventory here.
    from code_agent.codeintel.bundled_lsp import _launch
    for server in registry["servers"]:
        _launch(payload, server)
    write(payload / REGISTRY, registry)
    validation = validate(payload, registry, args.node)
    manifest["languageTools"] = REGISTRY
    write(manifest_path, manifest)
    write(previous_path, {"schemaVersion": 1, "groups": provenance,
                          "registrySha256": sha(payload / REGISTRY), "servers": sorted(expected)})
    return {"prepared": True, "payload": str(payload), "registry": REGISTRY, "servers": len(expected),
            "catalogServers": len(expected) + 1, "javaPreparedSeparately": True,
            "javascriptValidated": validation.get("javascriptValidated") is True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--node", type=Path)
    parser.add_argument("--ruby-build-tools", type=Path, help="Build-only MSYS2 UCRT64 root (GCC/make); not needed when merging prepared groups")
    parser.add_argument("--group-root", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--merge-existing", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-download", action="store_true")
    parser.add_argument("--existing-policy", choices=("compare", "error", "keep"), default="compare")
    print(json.dumps(prepare(parser.parse_args()), ensure_ascii=False), flush=True)

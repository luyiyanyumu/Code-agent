"""Build relocatable native Windows language tools from pinned official inputs.

No system installation, global environment changes or user LSP settings are used.
Ruby native gems require a build-machine MSYS2 UCRT64 GCC/make toolchain. Pass
--ruby-build-tools <MSYS2 root>; it is never shipped or required on user PCs.
MSYS2 is not installed automatically. Other runtimes are downloaded from locks.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile

LOCK = Path(__file__).with_name("language-native.lock.json")


def ruby_build_tools() -> Path | None:
    candidates = [os.environ.get("MSYS2_PATH"), os.environ.get("RI_DEVKIT"),
                  str(Path.home() / ".code-agent/ls/ruby/msys64"), "C:/msys64"]
    for value in candidates:
        if value:
            root = Path(value)
            if (root / "ucrt64/bin/gcc.exe").is_file() and (root / "usr/bin/make.exe").is_file():
                return root
    return None


def sha(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def prune_source_metadata(tools: Path) -> list[str]:
    """Exclude upstream checkout pointers and a gem developer-shell hook."""
    candidates = list((tools / "lua/meta/3rd").glob("*/.git"))
    candidates.append(tools / "ruby/lib/ruby/gems/3.4.0/gems/solargraph-0.61.0/.envrc")
    removed = []
    for path in candidates:
        if path.exists():
            if not path.resolve().is_relative_to(tools.resolve()) or not path.is_file() or path.is_symlink():
                raise ValueError("Unexpected upstream source metadata: " + str(path))
            removed.append(path.relative_to(tools).as_posix())
            path.unlink()
    return removed


def member(name: str) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if not path.parts or path.is_absolute() or ".." in path.parts or ":" in name:
        raise ValueError("Unsafe archive path: " + name)
    return path


def inventory(root: Path) -> dict[str, str]:
    candidates = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Runtime must not depend on filesystem links: " + str(path))
        if path.is_file():
            candidates.append(path)
    # Small runtime source/header files are numerous. Bounded parallel reads
    # avoid serial antivirus/file-open latency without weakening byte checks.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        hashes = pool.map(sha, candidates)
        return {path.relative_to(root).as_posix(): digest for path, digest in zip(candidates, hashes)}


def acquire(record: dict, cache: Path, *, download: bool) -> Path:
    path = cache / record["file"]
    if path.is_file() and sha(path) == record["sha256"]:
        return path
    if not download:
        raise ValueError("Missing or changed pinned input: " + str(path))
    cache.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    with urllib.request.urlopen(record["url"], timeout=90) as source, temporary.open("wb") as target:
        shutil.copyfileobj(source, target)
    if sha(temporary) != record["sha256"]:
        raise ValueError("Upstream input does not match lock: " + record["file"])
    temporary.replace(path)
    return path


def extract_zip(archive: Path, target: Path, strip: str = "") -> None:
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            relative = member(entry.filename)
            if strip:
                if relative.parts[0] != strip:
                    raise ValueError("Unexpected archive root: " + entry.filename)
                if len(relative.parts) == 1:
                    continue
                relative = PurePosixPath(*relative.parts[1:])
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("Archive links are unsupported")
            destination = target.joinpath(*relative.parts)
            if entry.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with package.open(entry) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)


def portable_ruby(archive: Path, target: Path, scratch: Path) -> None:
    # Windows ships bsdtar with 7z support. Validate the complete member list
    # before asking it to extract into a new scratch directory.
    tar = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/tar.exe")
    listing = subprocess.run([tar, "-tf", str(archive)], check=True, capture_output=True, text=True).stdout
    expected = "rubyinstaller-3.4.11-1-x64"
    for name in listing.splitlines():
        path = member(name)
        if path.parts[0] != expected:
            raise ValueError("Unexpected Ruby archive root")
    scratch.mkdir()
    # RI's tens of thousands of optional console documentation files are not
    # used by Solargraph (which ships RBS/YARD data). Preserve share/doc legal
    # notices, but avoid shipping or repeatedly hashing that optional RI tree.
    subprocess.run([tar, "-xf", str(archive), "-C", str(scratch), "--exclude", "*/share/ri/*"], check=True, capture_output=True)
    extracted = scratch / expected
    # Inspect links without hashing every file twice. The complete runtime is
    # hashed once into the final receipt after gem installation.
    for path in extracted.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Ruby archive must not depend on filesystem links")
    shutil.copytree(extracted, target)


def build_gems(ruby: Path, gems: list[dict], cache: Path, build_tools: Path | None, scratch: Path, *, download=False) -> None:
    if not build_tools or not (build_tools / "ucrt64/bin/gcc.exe").is_file() or not (build_tools / "usr/bin/make.exe").is_file():
        raise ValueError("Build-only prerequisite missing: MSYS2 UCRT64 GCC and make. Pass --ruby-build-tools <MSYS2 root>. End users do not need MSYS2.")
    gem_home = ruby / "lib/ruby/gems/3.4.0"
    gem_cache = gem_home / "cache"
    gem_cache.mkdir(parents=True, exist_ok=True)
    for record in gems:
        source = acquire(record, cache, download=download)
        destination = gem_cache / source.name
        if not destination.exists():
            shutil.copyfile(source, destination)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("RUBY", "GEM_", "BUNDLE_"))}
    isolated_home = scratch / "ruby-build-home"
    isolated_home.mkdir(exist_ok=True)
    empty_gemrc = isolated_home / ".gemrc"
    empty_gemrc.write_text("---\n", encoding="utf-8")
    system = Path(env.get("SystemRoot", "C:/Windows"))
    paths = [ruby / "bin", system / "System32", system]
    if build_tools:
        # Trusted local build tools are never copied into the distributable.
        paths[1:1] = [build_tools / "ucrt64/bin", build_tools / "usr/bin"]
        env.update({"MSYSTEM": "UCRT64", "RI_DEVKIT": str(build_tools), "MSYS2_PATH": str(build_tools)})
    env.update({"PATH": os.pathsep.join(map(str, paths)), "HOME": str(isolated_home),
                "USERPROFILE": str(isolated_home), "GEM_HOME": str(gem_home), "GEM_PATH": str(gem_home),
                "GEMRC": str(isolated_home / "empty-gemrc")})
    # --conservative keeps compatible prebuilt default gems (rbs/racc) rather
    # than pointlessly recompiling an unrelated newest cache entry.
    command = [str(ruby / "bin/ruby.exe"), str(ruby / "bin/gem"), "install", "solargraph",
               "--version", "0.61.0", "--local", "--conservative", "--no-document"]
    result = subprocess.run(command, cwd=gem_cache, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=900)
    (scratch / "ruby-build-output.txt").write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError("Ruby gem build failed; see " + str(scratch / "ruby-build-output.txt"))
    # Build logs and generated compiler scratch do not belong in the runtime.
    for path in ruby.rglob("*"):
        if path.is_file() and path.name in {"gem_make.out", "mkmf.log"}:
            path.unlink()


def path(relative: str, **extra) -> dict:
    return {"path": "runtimes/language-servers/" + relative, **extra}


def prepare_kotlin_stdlib(directory: Path) -> None:
    """Expose the packaged compiler stdlib through KLS's documented CLI lookup.

    This avoids overriding MAVEN_REPOSITORY, which would hide a project's real
    Maven dependencies. KLS finds kotlinc on its private child PATH and reads
    ../lib/kotlin-stdlib.jar without executing a shell during analysis.
    """
    # The server ZIP also carries an obsolete JNA 4.2.2 whose Windows native
    # library requires VC2010. Kotlin's compiler already embeds a compatible
    # newer JNA with an OS-only native library. Keep that single implementation
    # so wildcard classpath order cannot pick the obsolete DLL on clean PCs.
    old_jna = directory / "lib/jna-4.2.2.jar"
    if old_jna.exists():
        old_jna.unlink()
    shutil.copyfile(directory / "lib/kotlin-stdlib-2.1.0.jar", directory / "lib/kotlin-stdlib.jar")
    binary = directory / "bin"
    binary.mkdir(exist_ok=True)
    windows = '@echo off\r\n"%JAVA_HOME%\\bin\\java.exe" -cp "%~dp0..\\lib\\*" org.jetbrains.kotlin.cli.jvm.K2JVMCompiler %*\r\n'
    for name in ("kotlinc.bat", "kotlinc.cmd"):
        (binary / name).write_text(windows, encoding="utf-8", newline="")
    (binary / "kotlinc").write_text('#!/bin/sh\nexec "$JAVA_HOME/bin/java" -cp "$(dirname "$0")/../lib/*" org.jetbrains.kotlin.cli.jvm.K2JVMCompiler "$@"\n', encoding="utf-8")


def server_registry(include_ruby: bool = True) -> dict:
    base = "runtimes/language-servers/"
    servers = [
        {"id": "clangd", "version": "22.1.6", "command": base + "clangd/bin/clangd.exe",
         "args": ["--background-index=false", "--pch-storage=memory", "--log=error", "-j=2"],
         "requiredFiles": [base + "clangd/lib/clang/22/include/stddef.h", base + "zig/lib/libcxx/include/vector"],
         "initializationOptions": {"fallbackFlags": ["--target=x86_64-w64-windows-gnu", "-D_LIBCPP_HARDENING_MODE_DEFAULT=_LIBCPP_HARDENING_MODE_NONE", "-nostdinc", "-nostdinc++",
             "-isystem", path("zig/lib/libcxx/include"), "-isystem", path("zig/lib/libcxxabi/include"),
             "-isystem", path("clangd/lib/clang/22/include"), "-isystem", path("zig/lib/libc/include/any-windows-any"),
             "-isystem", path("zig/lib/include")]}, "env": {"XDG_CACHE_HOME": {"dataPath": "clangd/cache"}}},
        {"id": "kotlin", "version": "1.3.13", "command": "runtimes/java/bin/java.exe",
         "args": ["-Xms64m", "-Xmx512m", "-DkotlinLanguageServer.version=1.3.13", "-cp", path("kotlin/lib", suffix="/*"), "org.javacs.kt.MainKt"],
         "env": {"JAVA_HOME": {"path": "runtimes/java"}, "KOTLIN_HOME": path("kotlin"),
                 "XDG_CONFIG_HOME": {"dataPath": "kotlin/config"},
                 "PATH": {"paths": [base + "kotlin/bin", "runtimes/java/bin"], "inherit": True}},
         "initializationOptions": {"storagePath": {"dataPath": "kotlin/storage"}},
         "requiredFiles": [base + "kotlin/lib/server-1.3.13.jar", base + "kotlin/lib/kotlin-compiler-2.1.0.jar", base + "kotlin/lib/kotlin-stdlib.jar", base + "kotlin/bin/kotlinc.bat"]},
        {"id": "lua", "version": "3.19.1", "command": base + "lua/bin/lua-language-server.exe",
         "args": [{"dataPath": "lua/log", "prefix": "--logpath="}, {"dataPath": "lua/meta", "prefix": "--metapath="}],
         "requiredFiles": [base + "lua/main.lua", base + "lua/bin/main.lua"]},
        {"id": "terraform", "version": "0.39.0", "command": base + "terraform/terraform-ls.exe", "args": ["serve"]},
        {"id": "markdown", "version": "2026-02-08", "command": base + "markdown/marksman.exe", "args": ["server"]},
        {"id": "toml", "version": "0.10.0", "command": base + "toml/taplo.exe", "args": ["lsp", "stdio"],
         "initializationOptions": {"cachePath": {"dataPath": "toml/cache"},
             "settings": {"evenBetterToml": {"schema": {"catalogs": []}}}}},
        {"id": "zig", "version": "0.16.0", "command": base + "zls/zls.exe", "args": [],
         "requiredFiles": [base + "zig/zig.exe", base + "zig/lib/std/std.zig"],
         "env": {"PATH": {"paths": [base + "zig"], "inherit": True}, "ZIG_GLOBAL_CACHE_DIR": {"dataPath": "zig/global-cache"},
                 "ZIG_LOCAL_CACHE_DIR": {"dataPath": "zig/local-cache"}},
         "initializationOptions": {"zig_exe_path": path("zig/zig.exe"), "zig_lib_path": path("zig/lib"), "enable_build_on_save": False}},
        {"id": "dart", "version": "3.13.5", "command": base + "dart/bin/dart.exe", "args": ["language-server", "--protocol=lsp"],
         "requiredFiles": [base + "dart/bin/snapshots/analysis_server_aot.dart.snapshot", base + "dart/lib/core/core.dart"],
         "env": {"DART_SUPPRESS_ANALYTICS": "true", "DART_DISABLE_ANALYTICS": "true", "PUB_CACHE": {"dataPath": "dart/pub-cache"}}},
    ]
    if include_ruby:
        servers.append({"id": "ruby", "version": "3.4.11/solargraph0.61.0", "command": base + "ruby/bin/ruby.exe",
            "args": [path("ruby/bin/solargraph"), "stdio"],
            "env": {"GEM_HOME": path("ruby/lib/ruby/gems/3.4.0"), "GEM_PATH": path("ruby/lib/ruby/gems/3.4.0"),
                    "PATH": {"paths": [base + "ruby/bin"], "inherit": True}},
            "requiredFiles": [base + "ruby/bin/x64-ucrt-ruby340.dll", base + "ruby/lib/ruby/gems/3.4.0/gems/solargraph-0.61.0/lib/solargraph.rb"]})
    return {"schemaVersion": 1, "servers": servers}


def prepare(args) -> dict:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    output = args.output.resolve()
    receipt_file = output / "native-receipt.json"
    fragment_file = output / "native-fragment.json"
    tools = output / "runtimes/language-servers"
    if receipt_file.exists():
        old = json.loads(receipt_file.read_text(encoding="utf-8"))
        if not fragment_file.is_file() or json.loads(fragment_file.read_text(encoding="utf-8")) != server_registry(not args.skip_ruby):
            raise ValueError("Prepared native registry changed; use a new output directory")
        if old["lockSha256"] != sha(LOCK) or inventory(tools) != old["files"]:
            raise ValueError("Prepared native tools changed; use a new output directory")
        return {"prepared": True, "output": str(output), "files": len(old["files"]), "fragment": str(fragment_file), "cached": True}
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use a new/empty output directory")
    if not args.skip_ruby and (not args.ruby_build_tools or not (args.ruby_build_tools / "ucrt64/bin/gcc.exe").is_file()
                              or not (args.ruby_build_tools / "usr/bin/make.exe").is_file()):
        raise ValueError("Fresh Ruby preparation requires build-only MSYS2 UCRT64 GCC/make; pass --ruby-build-tools <MSYS2 root>. It is never needed on installed user machines.")
    output.mkdir(parents=True, exist_ok=True)
    tools.mkdir(parents=True)
    scratch = Path(tempfile.mkdtemp(prefix="native-language-build-", dir=output.parent))
    for record in sorted(lock["archives"], key=lambda record: record["id"] == "ruby"):
        if record["id"] in {"ruby-devkit", "solargraph"} or args.skip_ruby and record["id"] == "ruby":
            continue
        print(json.dumps({"stage": "prepare", "tool": record["id"]}), flush=True)
        source = acquire(record, args.cache, download=not args.offline)
        target = tools.joinpath(*member(record.get("target", record["id"])).parts)
        if record["id"] == "ruby":
            portable_ruby(source, target, scratch / "ruby-extract")
            build_gems(target, lock["gems"], args.gem_cache or args.cache, args.ruby_build_tools, scratch, download=not args.offline)
        else:
            extract_zip(source, target, record.get("strip", ""))
        if record["id"] == "kotlin":
            prepare_kotlin_stdlib(target)
    mark = lock["marksman"]
    mark_source = acquire(mark, args.cache, download=not args.offline)
    (tools / "markdown").mkdir()
    shutil.copyfile(mark_source, tools / "markdown/marksman.exe")
    for asset in lock.get("assets", []):
        source = acquire(asset, args.cache, download=not args.offline)
        target = tools.joinpath(*member(asset["target"]).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for notice in lock.get("notices", []):
        notice_source = acquire(notice, args.cache, download=not args.offline)
        notice_target = tools.joinpath(*member(notice["id"]).parts, *member(notice["file"]).parts)
        notice_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(notice_source, notice_target)
    fragment = server_registry(not args.skip_ruby)
    for record in fragment["servers"]:
        for relative in [record["command"], *record.get("requiredFiles", [])]:
            if relative.startswith("runtimes/java/"):
                continue
            if not (output / relative).is_file():
                raise ValueError("Prepared required file missing: " + relative)
    removed = prune_source_metadata(tools)
    receipt = {"schemaVersion": 1, "lockSha256": sha(LOCK), "files": inventory(tools), "prunedSourceMetadata": removed,
               "versions": {r["id"]: r["version"] for r in fragment["servers"]}, "utilities": lock.get("utilities", {})}
    receipt_file.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    fragment_file.write_text(json.dumps(fragment, indent=2) + "\n", encoding="utf-8")
    return {"prepared": True, "output": str(output), "files": len(receipt["files"]), "fragment": str(fragment_file), "scratch": str(scratch)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path.home() / ".code-agent/ls/_dl")
    parser.add_argument("--gem-cache", type=Path, help="Optional separate gem archive cache (defaults to --cache)")
    parser.add_argument("--ruby-build-tools", type=Path, default=ruby_build_tools(), help="Build-only MSYS2 UCRT64 root containing GCC and make; never copied into the runtime")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--skip-ruby", action="store_true")
    print(json.dumps(prepare(parser.parse_args())))

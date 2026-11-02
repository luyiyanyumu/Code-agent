"""Read-only discovery of the app's Java analysis tools and old managed installs.

The desktop manifest describes relocatable paths, never developer-machine paths.
Discovery only inspects a few known files; it does not run Java or install tools.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from itertools import islice
from pathlib import Path
from typing import Any

from code_agent.config import agent_home


def application_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _manifest() -> tuple[Path, dict[str, Any]]:
    root = application_root().resolve()
    try:
        with (root / "desktop-runtime.json").open("rb") as stream:
            raw = stream.read(65537)
        data = json.loads(raw.decode("utf-8-sig")) if len(raw) <= 65536 else {}
        return root, data if isinstance(data, dict) and data.get("schemaVersion") == 1 else {}
    except (OSError, ValueError, UnicodeError):
        return root, {}


def _contained(root: Path, relative: str) -> Path | None:
    path = (root / relative).resolve()
    return path if path.is_relative_to(root / "runtimes") else None


def _java_major(executable: Path) -> int:
    """JDK/JRE release files permit version checks without starting the JVM."""
    try:
        if not executable.is_file():
            return 0
        with (executable.parent.parent / "release").open("rb") as stream:
            release = stream.read(16385)
        if len(release) > 16384:
            return 0
        match = re.search(rb'(?m)^JAVA_VERSION="(\d+)(?:[."]|$)', release)
        return int(match[1]) if match else 0
    except (OSError, ValueError):
        return 0


def _launcher(directory: Path, *, contained: bool = False) -> Path | None:
    config = "config_win" if os.name == "nt" else "config_mac" if sys.platform == "darwin" else "config_linux"
    launcher = directory / "bin" / ("jdtls.bat" if os.name == "nt" else "jdtls")
    required = (launcher, directory / "bin/jdtls", directory / "bin/jdtls.py", directory / config / "config.ini")
    if not all(path.is_file() and (not contained or path.resolve().is_relative_to(directory)) for path in required):
        return None
    for pattern in ("org.eclipse.equinox.launcher_*.jar", "org.eclipse.jdt.ls.core_*.jar"):
        if not any(path.is_file() and (not contained or path.resolve().is_relative_to(directory))
                   for path in islice((directory / "plugins").glob(pattern), 2)):
            return None
    return launcher.resolve()


def bundled_tools() -> dict[str, Any] | None:
    root, manifest = _manifest()
    if manifest.get("java") != "runtimes/java/bin/java.exe" or manifest.get("jdtls") != "runtimes/jdtls":
        return None
    java = _contained(root, manifest["java"])
    jdt = _contained(root, manifest["jdtls"])
    if java is None or jdt is None or _java_major(java) < 21:
        return None
    launcher = _launcher(jdt, contained=True)
    # Validate symlink targets too: an app bundle cannot depend on an old install.
    if launcher is None or not launcher.is_relative_to(jdt):
        return None
    required = [jdt / "bin/jdtls", jdt / "bin/jdtls.py"]
    if any(not path.resolve().is_relative_to(jdt) for path in required):
        return None
    result: dict[str, Any] = {"command": str(launcher), "java": str(java), "source": "bundled"}
    if manifest.get("lombok") == "runtimes/lombok/lombok.jar":
        lombok = _contained(root, manifest["lombok"])
        if lombok is not None and lombok.is_file():
            result["lombok"] = str(lombok)
            versions = manifest.get("runtimeVersions") or {}
            if isinstance(versions, dict) and isinstance(versions.get("lombok"), str):
                result["lombok_version"] = versions["lombok"][:80]
    return result


def _local_java() -> Path | None:
    suffix = "java.exe" if os.name == "nt" else "java"
    candidates = []
    if os.environ.get("JAVA_HOME"):
        candidates.append(Path(os.environ["JAVA_HOME"]) / "bin" / suffix)
    found = shutil.which("java")
    if found:
        candidates.append(Path(found))
    return next((path.resolve() for path in candidates if _java_major(path.resolve()) >= 21), None)


def default_launch() -> dict[str, Any] | None:
    tools = bundled_tools()
    if tools is not None:
        return tools
    # Preserve a system/package-manager launcher when no app bundle is present.
    if shutil.which("jdtls"):
        return None
    # These were managed tool roots before the desktop data directory changed.
    # Do not import arbitrary old lsp.json settings or inspect unrelated folders.
    java = _local_java()
    if java is None:
        return None
    for base in dict.fromkeys((agent_home(), Path.home() / ".code-agent", Path.home() / ".ch-agent")):
        launcher = _launcher(base / "ls/jdtls")
        if launcher:
            return {"command": str(launcher), "java": str(java), "source": "managed-local"}
    return None


def has_explicit_java(args: list[str], env: dict[str, Any]) -> bool:
    return bool(env.get("JAVA_HOME")) or any(
        arg == "--java-executable" or arg.startswith("--java-executable=") for arg in args
    )

"""Resolve packaged language servers without a system install or PATH mutation.

The build produces ``runtimes/language-tools.json``. Executables and typed path
values are relative to the app, so a relocated installation stays self-contained.
Only the server's subprocess receives its SDK/cache environment.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any

from code_agent.config import agent_home

REGISTRY_PATH = "runtimes/language-tools.json"
_MAX_REGISTRY_BYTES = 1024 * 1024
_ID = re.compile(r"[a-z][a-z0-9_-]{0,79}\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


def application_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_json(path: Path, limit: int) -> Any:
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("Language tools registry is too large")
    return json.loads(value.decode("utf-8-sig"))


def _parts(value: Any) -> tuple[str, ...]:
    if (not isinstance(value, str) or not value or ":" in value
            or any(ord(character) < 32 for character in value)
            or PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()):
        raise ValueError("Invalid relative language tool path")
    parts = PurePosixPath(value.replace("\\", "/")).parts
    if not parts or ".." in parts:
        raise ValueError("Language tool path leaves its root")
    return parts


def _runtime_path(root: Path, value: Any, *, kind: str = "any") -> str:
    parts = _parts(value)
    if parts[0] != "runtimes" or len(parts) < 2:
        raise ValueError("Language tools must be inside the bundled runtimes")
    candidate = root.joinpath(*parts).resolve()
    # Check resolved targets, not just lexical paths or registry declarations.
    if not candidate.is_relative_to(root / "runtimes"):
        raise ValueError("Language tool points outside the application")
    if kind == "file" and not candidate.is_file():
        raise ValueError("Bundled language tool file is missing")
    if kind == "directory" and not candidate.is_dir():
        raise ValueError("Bundled language tool directory is missing")
    if kind == "any" and not candidate.exists():
        raise ValueError("Bundled language tool path is missing")
    return str(candidate)


def _data_path(value: Any) -> str:
    parts = _parts(value)
    if parts[0] == "language-servers":
        parts = parts[1:]
    if not parts:
        raise ValueError("Language cache path needs a server-specific name")
    home = agent_home().resolve()
    base = (home / "language-servers").resolve()
    if not base.is_relative_to(home):
        raise ValueError("Language cache root leaves the user data directory")
    candidate = base.joinpath(*parts).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError("Language cache path leaves the user data directory")
    return str(candidate)


def _template(root: Path, value: Any, *, depth: int = 0) -> Any:
    if depth > 24:
        raise ValueError("Language configuration nesting is too deep")
    if isinstance(value, dict):
        kinds = {"path", "dataPath"}.intersection(value)
        # Some real LSP option objects are named "path" themselves (for
        # example tsserver.path). A nested object is a normal container; the
        # inner scalar typed-path value is the template to resolve.
        if (len(kinds) == 1 and set(value) <= {*kinds, "prefix", "suffix"}
                and not isinstance(value[next(iter(kinds))], dict)):
            kind = next(iter(kinds))
            path = _runtime_path(root, value[kind]) if kind == "path" else _data_path(value[kind])
            prefix, suffix = value.get("prefix", ""), value.get("suffix", "")
            if any(not isinstance(text, str) or len(text) > 512 or any(ord(char) < 32 for char in text)
                   for text in (prefix, suffix)):
                raise ValueError("Invalid language tool path decoration")
            if ":" in suffix or ".." in PurePosixPath(suffix.replace("\\", "/")).parts:
                raise ValueError("Language tool suffix leaves the validated path")
            return prefix + path + suffix
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Language configuration keys must be strings")
        return {key: _template(root, item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        if len(value) > 2048:
            raise ValueError("Too many language configuration values")
        return [_template(root, item, depth=depth + 1) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError("Invalid language configuration value")


def _environment(root: Path, raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or len(raw) > 128:
        raise ValueError("Invalid language tool environment")
    result = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not _ENV.fullmatch(key):
            raise ValueError("Invalid language tool environment variable")
        if isinstance(value, dict) and "paths" in value:
            if set(value) - {"paths", "inherit"} or not isinstance(value["paths"], list):
                raise ValueError("Invalid language tool search path")
            if len(value["paths"]) > 128 or not isinstance(value.get("inherit", False), bool):
                raise ValueError("Invalid language tool search path")
            pieces = [_runtime_path(root, path, kind="directory") for path in value["paths"]]
            if value.get("inherit") and os.environ.get(key):
                pieces.append(os.environ[key])
            resolved = os.pathsep.join(pieces)
        else:
            resolved = _template(root, value)
        if not isinstance(resolved, str) or "\x00" in resolved:
            raise ValueError("Language tool environment values must be strings")
        result[key] = resolved
    return result


def _launch(root: Path, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not _ID.fullmatch(str(raw.get("id") or "")):
        raise ValueError("Invalid bundled language server ID")
    command = _runtime_path(root, raw.get("command"), kind="file")
    args = raw.get("args", [])
    if not isinstance(args, list) or len(args) > 2048:
        raise ValueError("Invalid bundled language server arguments")
    args = _template(root, args)
    if any(not isinstance(arg, str) or "\x00" in arg for arg in args):
        raise ValueError("Language server arguments must be strings")
    for field, kind in (("requiredFiles", "file"), ("requiredDirectories", "directory")):
        paths = raw.get(field, [])
        if not isinstance(paths, list) or len(paths) > 8192:
            raise ValueError("Invalid language tool dependency list")
        for path in paths:
            _runtime_path(root, path, kind=kind)
    options = _template(root, raw.get("initializationOptions", {}))
    if not isinstance(options, dict):
        raise ValueError("Language initialization options must be an object")
    version = raw.get("version", "")
    if not isinstance(version, str) or len(version) > 160:
        raise ValueError("Invalid bundled language tool version")
    result = {"id": raw["id"], "command": command, "args": args,
              "env": _environment(root, raw.get("env", {})),
              "initialization_options": options, "version": version, "source": "bundled"}
    if isinstance(raw.get("name"), str) and raw["name"]:
        result["name"] = raw["name"][:160]
    return result


def launches() -> dict[str, dict[str, Any]]:
    """Return complete, relocatable server definitions; invalid entries stay unavailable.

    Read once for the whole catalog. No launch, download, directory creation or
    permanent environment change is performed during detection.
    """
    root = application_root().resolve()
    try:
        manifest = _read_json(root / "desktop-runtime.json", 65536)
        if (not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1
                or manifest.get("languageTools") != REGISTRY_PATH):
            return {}
        registry = _read_json(Path(_runtime_path(root, REGISTRY_PATH, kind="file")), _MAX_REGISTRY_BYTES)
        if not isinstance(registry, dict) or registry.get("schemaVersion") != 1:
            return {}
        servers = registry.get("servers")
        if not isinstance(servers, list) or len(servers) > 128:
            return {}
        result = {}
        seen = set()
        for raw in servers:
            sid = raw.get("id") if isinstance(raw, dict) else None
            if isinstance(sid, str) and sid in seen:
                # A duplicate ID is ambiguous; never select whichever happens last.
                return {}
            if isinstance(sid, str):
                seen.add(sid)
            try:
                launch = _launch(root, raw)
            except (OSError, ValueError, TypeError, RuntimeError):
                continue
            result[launch["id"]] = launch
        return result
    except (OSError, ValueError, UnicodeError, TypeError, RuntimeError):
        return {}


def merge_options(base: dict, extra: dict) -> dict:
    """Merge fresh default objects without mutating registry or user settings."""
    result = dict(base)
    for key, value in extra.items():
        result[key] = merge_options(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
    return result

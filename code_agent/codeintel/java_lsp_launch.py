"""Prepare JDTLS arguments without running Java, Maven, or downloading dependencies.

Lombok's Eclipse integration is a JVM agent, not merely a Maven annotation
processor. Resolve only bounded project/cached POMs and an existing Lombok jar.
User options win; status contains fixed explanations, never POM/config contents.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_POM_BYTES = 1024 * 1024
MAX_POM_FILES = 64
MAX_POM_TOTAL_BYTES = 8 * MAX_POM_BYTES
MAX_PROJECT_DIRS = 256
MAX_PARENT_DEPTH = 12
LOMBOK = ("org.projectlombok", "lombok")
_PROPERTY = re.compile(r"\$\{([^{}]+)\}")
_COORDINATE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+\-]*\Z")
_SKIP_DIRS = {".git", ".idea", ".metadata", "node_modules", "target", "build", "dist", ".venv", "venv"}


def _inside(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


def _text(node: ET.Element | None, key: str) -> str:
    if node is None:
        return ""
    return (node.findtext(key) or "").strip()


def _expand(value: str, properties: dict[str, str]) -> str:
    for _ in range(12):
        updated = _PROPERTY.sub(lambda match: properties.get(match[1], match[0]), value)
        if updated == value:
            break
        value = updated
    return value if "${" not in value else ""


def _coordinate_path(repository: Path, group: str, artifact: str, version: str, suffix: str) -> Path | None:
    if not all(_COORDINATE.fullmatch(part) and part not in {".", ".."} for part in (group, artifact, version)):
        return None
    candidate = (repository / group.replace(".", "/") / artifact / version / f"{artifact}-{version}.{suffix}").resolve()
    return candidate if _inside(candidate, repository) else None


@dataclass
class _Pom:
    properties: dict[str, str] = field(default_factory=dict)
    managed: dict[tuple[str, str], str] = field(default_factory=dict)
    used: bool = False
    version: str = ""
    coordinates: tuple[str, str, str] = ("", "", "")


class _MavenCache:
    """A small offline subset of Maven inheritance, sufficient for Lombok versions."""

    def __init__(self, root: Path, repository: Path) -> None:
        self.root, self.repository = root, repository
        self.cache: dict[Path, _Pom] = {}
        self.read_bytes = 0
        self.rejected = False

    def read(self, path: Path, depth: int = 0) -> _Pom:
        path = path.resolve()
        if path in self.cache:
            return self.cache[path]
        if depth > MAX_PARENT_DEPTH or len(self.cache) >= MAX_POM_FILES or not (
            _inside(path, self.root) or _inside(path, self.repository)
        ):
            self.rejected = True
            return _Pom()
        # An early sentinel also breaks parent/BOM cycles.
        self.cache[path] = _Pom()
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_POM_BYTES + 1)
            self.read_bytes += len(data)
            # Removing NULs also catches DTD/entity declarations in UTF-16/32 XML.
            declarations = data.replace(b"\x00", b"").upper()
            if (len(data) > MAX_POM_BYTES or self.read_bytes > MAX_POM_TOTAL_BYTES
                    or b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations):
                self.rejected = True
                return _Pom()
            xml = ET.fromstring(data)
            if len(list(xml.iter())) > 20000:
                self.rejected = True
                return _Pom()
            for node in xml.iter():
                node.tag = node.tag.rsplit("}", 1)[-1]
        except FileNotFoundError:
            return _Pom()
        except (OSError, ET.ParseError, ValueError):
            self.rejected = True
            return _Pom()

        own = {node.tag: (node.text or "").strip() for node in xml.findall("properties/*")}
        parent_node = xml.find("parent")
        parent = _Pom()
        if parent_node is not None:
            relative_node = parent_node.find("relativePath")
            relative = "../pom.xml" if relative_node is None else (relative_node.text or "").strip()
            local_parent = (path.parent / relative).resolve() if relative else None
            coordinate = tuple(_expand(_text(parent_node, key), own) for key in ("groupId", "artifactId", "version"))
            local_model = self.read(local_parent, depth + 1) if (
                local_parent and _inside(local_parent, self.root) and local_parent.is_file()
            ) else _Pom()
            # Maven falls back to the repository when relativePath points at a different project.
            if all(coordinate) and local_model.coordinates == coordinate:
                parent = local_model
            else:
                cached_parent = _coordinate_path(self.repository, *coordinate, "pom")
                if cached_parent:
                    parent = self.read(cached_parent, depth + 1)
        properties = {**parent.properties, **own}
        for key in ("groupId", "artifactId", "version"):
            value = _text(xml, key) or _text(parent_node, key)
            properties[f"project.{key}"] = value
            properties[f"pom.{key}"] = value
        managed = dict(parent.managed)
        own_managed = False
        entries = xml.findall("dependencyManagement/dependencies/dependency")
        for item in entries:
            coordinate = tuple(_expand(_text(item, key), properties) for key in ("groupId", "artifactId"))
            if coordinate == LOMBOK:
                managed[LOMBOK] = _text(item, "version")
                own_managed = True
        # Avoid walking all Spring Boot BOMs when its own managed Lombok already answers.
        if not own_managed:
            for item in entries[:MAX_POM_FILES]:
                if _text(item, "scope") != "import" or _text(item, "type") != "pom":
                    continue
                coordinate = [_expand(_text(item, key), properties) for key in ("groupId", "artifactId", "version")]
                bom_path = _coordinate_path(self.repository, *coordinate, "pom")
                if bom_path:
                    bom = self.read(bom_path, depth + 1)
                    if LOMBOK in bom.managed:
                        managed[LOMBOK] = _expand(bom.managed[LOMBOK], bom.properties)
                        break
        dependencies = xml.findall("dependencies/dependency")
        dependencies += xml.findall("build/plugins/plugin/configuration/annotationProcessorPaths/path")
        dependencies += xml.findall("build/pluginManagement/plugins/plugin/configuration/annotationProcessorPaths/path")
        used, version = parent.used, parent.version
        for item in dependencies:
            coordinate = tuple(_expand(_text(item, key), properties) for key in ("groupId", "artifactId"))
            if coordinate == LOMBOK:
                used = True
                version = _text(item, "version") or managed.get(LOMBOK, "")
        coordinates = tuple(_expand(properties[f"project.{key}"], properties) for key in ("groupId", "artifactId", "version"))
        model = _Pom(properties, managed, used, version, coordinates)
        self.cache[path] = model
        return model

    def project_versions(self) -> tuple[bool, set[str]]:
        used, versions = False, set()
        visited = 0
        for current, directories, files in os.walk(self.root, followlinks=False):
            visited += 1
            base = Path(current)
            depth = len(base.relative_to(self.root).parts)
            directories[:] = sorted(name for name in directories if name not in _SKIP_DIRS
                                     and not name.startswith(".") and not (base / name).is_symlink()) if depth < 4 else []
            if visited > MAX_PROJECT_DIRS or len(self.cache) >= MAX_POM_FILES:
                self.rejected = True
                break
            if "pom.xml" in files:
                model = self.read(base / "pom.xml")
                used |= model.used
                if model.used:
                    version = _expand(model.version or model.managed.get(LOMBOK, ""), model.properties)
                    if version and _COORDINATE.fullmatch(version):
                        versions.add(version)
        return used, versions


def _lombok_jar(path: Path) -> bool:
    """Do not turn an unrelated cached jar into an automatically loaded agent."""
    try:
        if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            return False
        with zipfile.ZipFile(path) as archive:
            manifest = archive.getinfo("META-INF/MANIFEST.MF")
            if manifest.file_size > 16384:
                return False
            data = archive.read(manifest).decode("utf-8", errors="replace")
        return bool(re.search(r"(?im)^Premain-Class:\s*lombok\.launch\.Agent\s*$", data))
    except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile, KeyError):
        return False


def _existing_agent(args: list[str], env: dict[str, Any]) -> bool:
    for arg in args:
        value = arg.removeprefix("--jvm-arg=")
        if value.startswith("-javaagent:"):
            path = value[len("-javaagent:"):].strip('"\'')
            if "lombok" in path.lower() or _lombok_jar(Path(path)) or _lombok_jar(Path(path.split("=", 1)[0])):
                return True
    # Environment values are inspected only for agent presence, never returned/logged.
    for key in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS"):
        value = str(env.get(key) or "")
        for match in re.finditer(r'''(?:^|\s)-javaagent:(?:"([^"]+)"|'([^']+)'|([^\s]+))''', value):
            path = next(part for part in match.groups() if part is not None)
            if "lombok" in path.lower() or _lombok_jar(Path(path)) or _lombok_jar(Path(path.split("=", 1)[0])):
                return True
    return False


def jdtls_process_command(command: str, args: list[str]) -> tuple[str, list[str]]:
    """Bypass only the official Windows Python shim, preserving custom launchers.

    Windows can implicitly run .bat through cmd.exe even with subprocess_exec.
    Passing the same Python entry point directly keeps paths/metacharacters as
    argv and avoids the official batch file's interactive `pause` on exit.
    """
    launcher = Path(command)
    if launcher.suffix.lower() not in {".bat", ".cmd"}:
        return command, list(args)
    script = launcher.parent / "jdtls"
    if not script.is_file() or not (launcher.parent / "jdtls.py").is_file():
        return command, list(args)
    try:
        with launcher.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            return command, list(args)
        lines = [line.strip().lower() for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    except (OSError, UnicodeError):
        return command, list(args)
    expected = ['@echo off', 'python "%~dp0/jdtls" %*']
    if len(lines) in {2, 3} and lines[:2] == expected and (len(lines) == 2 or lines[2] == "pause"):
        return sys.executable, [str(script), *args]
    return command, list(args)


def _workspace_args(args: list[str], project: dict, tree: str, entry: dict, data_root: Path) -> tuple[list[str], dict]:
    result, base = [], None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-data" and index + 1 < len(args):
            base = args[index + 1]
            index += 2
        elif arg.startswith("-data="):
            base = arg[6:]
            index += 1
        else:
            result.append(arg)
            index += 1
    explicit = base is not None
    if explicit and entry.get("isolate_workspace") is False:
        return list(args), {"isolated": False, "source": "configured", "reason": "使用用户指定的完整 JDTLS 数据目录"}
    base_path = Path(base).expanduser() if base else data_root / "jdtls"
    if not base_path.is_absolute():
        base_path = Path(project["root_path"]) / base_path
    identity = "\x00".join((str(project.get("id") or ""), os.path.normcase(str(Path(project["root_path"]).resolve())), tree))
    child = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    workspace = base_path.resolve() / "workspaces" / child
    result.extend(["-data", str(workspace)])
    return result, {"isolated": True, "source": "configured-root" if explicit else "default", "path": str(workspace)}


def prepare_jdtls_launch(project: dict, tree: str, entry: dict, command: str, *,
                         repository: Path | None = None, data_root: Path | None = None) -> tuple[list[str], dict]:
    """Return argv + safe, explainable environment metadata; never create files."""
    from code_agent.config import agent_home

    root = Path(project["root_path"]).resolve()
    args, workspace = _workspace_args(list(entry.get("args") or []), project, tree, entry,
                                       data_root or agent_home() / "lsp-workspaces")
    state: dict[str, Any] = {"enabled": False, "status": "not-required", "source": "none", "reason": "项目未声明 Lombok"}
    metadata = {"workspace_data": workspace, "lombok": state}
    if _existing_agent(args, entry.get("env") or {}) or _existing_agent([], os.environ):
        state.update(enabled=True, status="configured", source="jvm-argument", reason="保留已有 Lombok JVM agent，不重复注入")
        return args, metadata
    if entry.get("lombok_enabled") is False:
        state.update(status="disabled", source="configured", reason="用户已禁用自动 Lombok 支持")
        return args, metadata

    explicit = str(entry.get("lombok_jar") or "").strip()
    jar = None
    version = ""
    if explicit:
        candidate = Path(explicit).expanduser()
        candidate = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if _lombok_jar(candidate):
            jar = candidate
        state.update(source="configured", status="missing", reason="指定的 Lombok jar 不存在或不含 Lombok agent")
    else:
        configured_repo = entry.get("maven_repository")
        repo = Path(repository or configured_repo or Path.home() / ".m2" / "repository").expanduser()
        if not repo.is_absolute():
            repo = root / repo
        repo = repo.resolve()
        cache = _MavenCache(root, repo)
        used, versions = cache.project_versions()
        if not used:
            if cache.rejected:
                state.update(status="unresolved", reason="部分 Maven 配置不可安全读取；可通过 lombokJar 指定本地 jar")
            return args, metadata
        if len(versions) > 1:
            state.update(status="unresolved", reason="项目模块声明不同 Lombok 版本；请通过 lombokJar 明确分析使用的版本")
            return args, metadata
        if versions:
            version = next(iter(versions))
            candidate = _coordinate_path(repo, *LOMBOK, version, "jar")
            if candidate and _lombok_jar(candidate):
                jar = candidate
            state.update(source="maven-cache", version=version, status="missing", reason="匹配项目版本的 Lombok jar 未缓存；保持普通 Java 分析，可指定 lombokJar")
        else:
            state.update(status="unresolved", reason="离线 Maven 配置未能确定 Lombok 版本；请通过 lombokJar 指定本地 jar")
            return args, metadata
    if jar is None:
        return args, metadata

    agent = f"-javaagent:{jar}"
    name = Path(command).name.lower()
    if name in {"java", "java.exe"} and "-jar" in args:
        args.insert(args.index("-jar"), agent)
    elif Path(command).stem.lower() == "jdtls" or any(Path(arg).name.lower() in {"jdtls", "jdtls.py"} for arg in args):
        args.append(f"--jvm-arg={agent}")
    else:
        state.update(status="unsupported-launcher", reason="自定义 launcher 未声明 JVM 参数接口；请自行配置 Lombok JVM agent")
        return args, metadata
    state.update(enabled=True, status="enabled", path=str(jar), reason="已为 JDTLS 启用本地 Lombok JVM agent")
    return args, metadata

"""Bounded frontend previews and explicitly saved design-only styles.

Preview tickets are short-lived capabilities for a single owned project. They
never contain the application's bearer token or forward cookies/credentials.
Frontend assets and explicitly configured read-only project APIs have separate
capabilities; neither proxy carries the code assistant's credentials.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit, urlunsplit

import httpx

from code_agent.codeintel.paths import safe_child

DESIGN_CSS = "code-agent.design.css"
MAX_TEXT = 2 * 1024 * 1024
MAX_ASSET = 8 * 1024 * 1024
TICKET_TTL = 15 * 60
MAX_TICKETS = 64
MAX_DISCOVERY_DIRECTORIES = 2000
MAX_DISCOVERY_ENTRIES = 80
ASSET_ROUTE = "/api/v1/code/frontend-design-assets/"
API_ROUTE = "/api/v1/code/frontend-design-api/"
_DISCOVERY_SKIP = {"node_modules", ".git", ".hg", ".svn", ".venv", "venv", "__pycache__", "coverage", "target", ".next", ".nuxt"}
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db", ".log", ".toml", ".yaml", ".yml"}
_ASSET_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".json", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".mp3", ".mp4", ".webm", ".ogg", ".wav", ".wasm", ".map"}
_HMR_STUB = b'export function createHotContext(){return {data:{},accept(){},acceptExports(){},dispose(){},prune(){},decline(){},invalidate(){},on(){},off(){},send(){}}}; export function injectQuery(u){return u}; export function updateStyle(id,css){let n=document.getElementById(id);if(!n){n=document.createElement("style");n.id=id;document.head.append(n)}n.textContent=css}; export function removeStyle(id){document.getElementById(id)?.remove()};'
_PREFLIGHT_TEXT = 256 * 1024
_SOURCE_MODULE_SUFFIXES = {".ts", ".tsx", ".jsx", ".vue", ".svelte"}


class DesignError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _parts(raw: str, *, live: bool = False) -> str:
    text = unquote(raw).replace("\\", "/")
    if len(text) > 4096 or any(ord(c) < 32 for c in text) or "\x00" in text:
        raise DesignError("资源路径不合法")
    parts = [part for part in text.split("/") if part]
    for index, part in enumerate(parts):
        vite_cache = live and part == ".vite" and index > 0 and parts[index - 1] == "node_modules" and parts[index + 1:index + 2] == ["deps"]
        # Vite's standard Vue virtual export helper is a module ID, not a
        # filesystem path. Only bounded plugin/virtual IDs in /@id are allowed;
        # drive letters, nested paths and all /@fs requests remain forbidden.
        virtual_id = live and index == 1 and len(parts) == 2 and parts[0] == "@id" and len(part) <= 256 and re.fullmatch(r"(?:__x00__)?(?:plugin-[A-Za-z0-9_-]+|virtual):[A-Za-z0-9_.-]+", part) is not None
        if part in {"..", "."} or (part.startswith(".") and not vite_cache) or (":" in part and not virtual_id):
            raise DesignError("不允许访问隐藏文件或越界路径")
    # src/api is a common public client-module directory. It is distinct from
    # the /api transport endpoint: only explicitly named source modules are
    # permitted here, and the live response must also be JavaScript below.
    module_suffix = Path(parts[-1]).suffix.lower() if parts else ""
    client_module = module_suffix in {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte"}
    forbidden_api = any(part.lower() == "api" and not (client_module and index > 0 and parts[index - 1].lower() == "src") for index, part in enumerate(parts))
    if forbidden_api or any(p.lower() in {"@fs", "__open-in-editor", "__vite_ping", "credentials", "secrets"} for p in parts):
        raise DesignError("预览仅支持前端资源，不转发 API 或工作区外资源")
    name = parts[-1].lower() if parts else ""
    if name.startswith("env") and name in {"env", "environment", "env.json", "environment.json"}:
        raise DesignError("不允许预览环境配置")
    if Path(name).suffix in _SECRET_SUFFIXES or any(word in name for word in ("credential", "secret", "private-key")):
        raise DesignError("不允许预览凭据或配置文件")
    return "/".join(parts)


def _target(root: Path, rel: str, *, must_exist: bool = True) -> Path:
    clean = _parts(rel)
    if not clean:
        raise DesignError("请选择 HTML 入口")
    target = safe_child(root, clean)
    # Symlinks inside the root are also excluded: the resource capability must
    # not follow a link subsequently redirected to private data.
    current = root
    for part in clean.split("/"):
        current = current / part
        if current.is_symlink():
            raise DesignError("预览不支持符号链接资源")
    if must_exist and not target.is_file():
        raise DesignError("前端资源不存在", 404)
    return target


def _read(target: Path, limit: int = MAX_TEXT) -> bytes:
    with target.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise DesignError("前端资源超过预览大小限制", 413)
    return data


def _text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DesignError("入口和样式需使用 UTF-8 文本") from exc


def _loopback_url(raw: str, blocked_ports: set[int]) -> str:
    try:
        parsed = urlsplit(raw.strip())
        if parsed.scheme != "http" or parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise DesignError("预览地址只接受无凭据的本机 HTTP 地址")
        host = parsed.hostname or ""
        # localhost is pinned to a literal address, avoiding DNS rebinding and
        # environment proxy resolution for every subsequent asset request.
        address = ipaddress.ip_address("127.0.0.1" if host.lower() == "localhost" else host)
        if not address.is_loopback:
            raise DesignError("预览地址必须是本机 loopback 服务")
        port = parsed.port or 80
        if port in blocked_ports:
            raise DesignError("不能预览当前代码助手前后端端口，请使用项目自己的开发服务")
        # The initial HTML fetch has exactly the same path boundary as assets;
        # checking the content type after a private/API GET would be too late.
        _parts(parsed.path, live=True)
        authority = f"[{address}]" if address.version == 6 else str(address)
        return urlunsplit(("http", f"{authority}:{port}", parsed.path or "/", parsed.query, ""))
    except (ValueError, TypeError) as exc:
        if isinstance(exc, DesignError):
            raise
        raise DesignError("预览地址不是有效的本机 HTTP 地址") from exc


def _api_parts(raw: str) -> str:
    """Validate API paths before URL parsers can collapse traversal segments."""
    if len(raw) > 4096 or "\\" in raw or any(ord(c) < 32 for c in raw):
        raise DesignError("项目接口路径不合法")
    text = raw
    for _ in range(5):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    if "%" in text or any(c in text for c in "\\?#"):
        raise DesignError("项目接口路径不能包含嵌套编码或 URL 参数")
    parts = [part for part in text.split("/") if part]
    forbidden = {"@fs", "@id", "__open-in-editor", "__vite_ping", "credentials", "secrets", "env", "environment"}
    for part in parts:
        lower = part.lower()
        if part in {".", ".."} or part.startswith(".") or ":" in part or any(ord(c) < 32 for c in part):
            raise DesignError("项目接口不允许隐藏文件或越界路径")
        if lower in forbidden or Path(lower).suffix in _SECRET_SUFFIXES or any(word in lower for word in ("credential", "secret", "private-key")):
            raise DesignError("项目接口不允许访问凭据或敏感路径")
    return "/".join(parts)


def _loopback_api_url(raw: str, blocked_ports: set[int]) -> str:
    try:
        text = raw.strip()
        if len(raw) > 4096 or any(ord(c) < 32 for c in raw) or "?" in text or "#" in text or "\\" in text:
            raise DesignError("项目接口地址不能包含查询参数、片段或控制字符")
        parsed = urlsplit(text)
        if parsed.scheme != "http" or parsed.username is not None or parsed.password is not None:
            raise DesignError("项目接口只接受无凭据的本机 HTTP 地址")
        host = parsed.hostname or ""
        address = ipaddress.ip_address("127.0.0.1" if host.lower() == "localhost" else host)
        if not address.is_loopback:
            raise DesignError("项目接口地址必须是本机 loopback 服务")
        port = parsed.port if parsed.port is not None else 80
        if port == 0:
            raise DesignError("项目接口端口必须在 1 到 65535 之间")
        if port in blocked_ports | {8765, 5175}:
            raise DesignError("不能连接代码助手前后端端口，请使用项目自己的后端接口")
        path = _api_parts(parsed.path)
        if path and path.split("/", 1)[0].lower() != "api":
            raise DesignError("项目接口地址必须使用 /api 接口前缀")
        authority = f"[{address}]" if address.version == 6 else str(address)
        return urlunsplit(("http", f"{authority}:{port}", "/" + quote(path or "api", safe="/") + "/", "", ""))
    except (ValueError, TypeError) as exc:
        if isinstance(exc, DesignError):
            raise
        raise DesignError("项目接口不是有效的本机 HTTP 地址") from exc


def _api_query(raw: str) -> str:
    if len(raw) > 8192 or any(ord(c) < 32 for c in raw):
        raise DesignError("项目接口查询参数过长或不合法")
    pairs = parse_qsl(raw, keep_blank_values=True)
    if len(pairs) > 64:
        raise DesignError("项目接口查询参数过多")
    for key, _ in pairs:
        name = re.sub(r"[^a-z0-9]", "", key.lower())
        if any(part in name for part in ("authorization", "cookie", "password", "credential", "secret", "token", "apikey", "sessionid")):
            raise DesignError("项目接口预览不转发凭据查询参数")
    return raw


def _js_tokens(source: str) -> list[tuple[str, str]]:
    """A bounded lexical scan, never an evaluator or a scan of comment text."""
    tokens: list[tuple[str, str]] = []
    position = 0
    source = source[:_PREFLIGHT_TEXT]
    word = re.compile(r"[A-Za-z_$][\w$]*|\d+")
    while position < len(source) and len(tokens) < 24000:
        char = source[position]
        if char.isspace():
            position += 1
            continue
        if source.startswith("//", position):
            end = source.find("\n", position + 2)
            position = len(source) if end < 0 else end + 1
            continue
        if source.startswith("/*", position):
            end = source.find("*/", position + 2)
            position = len(source) if end < 0 else end + 2
            continue
        if char in "'\"`":
            quote_char, start = char, position + 1
            position += 1
            while position < len(source):
                if source[position] == "\\":
                    position += 2
                elif source[position] == quote_char:
                    break
                else:
                    position += 1
            value = source[start:position]
            tokens.append(("template" if char == "`" else "string", value))
            position += 1
            continue
        # Ignore regex literal contents as well as strings. Slash after an
        # identifier/closing expression is division, not the start of regex.
        if char == "/" and (not tokens or tokens[-1][1] in {"=", "(", "[", ":", ",", ";", "!", "?", "return", "=>"}):
            position += 1
            in_class = False
            while position < len(source):
                current = source[position]
                if current == "\\":
                    position += 2
                    continue
                if current == "[":
                    in_class = True
                elif current == "]":
                    in_class = False
                elif current == "/" and not in_class:
                    position += 1
                    break
                elif current in "\r\n":
                    break
                position += 1
            while position < len(source) and source[position].isalpha():
                position += 1
            tokens.append(("regex", ""))
            continue
        match = word.match(source, position)
        if match:
            value = match[0]
            tokens.append(("number" if value[0].isdigit() else "id", value))
            position += len(value)
        else:
            tokens.append(("punct", char))
            position += 1
    return tokens


def _module_imports(source: str) -> list[str]:
    tokens = _js_tokens(source)
    imports = []
    for index, token in enumerate(tokens):
        if token[0] != "id" or token[1] not in {"import", "export"} or (index and tokens[index - 1][1] == "."):
            continue
        rest = tokens[index + 1:index + 81]
        if token[1] == "import" and rest:
            if rest[0][0] == "string":
                imports.append(rest[0][1])
                continue
            if len(rest) > 1 and rest[0][1] == "(" and rest[1][0] == "string":
                imports.append(rest[1][1])
                continue
            if rest[0][1] in {".", "("}:
                continue
        for offset, current in enumerate(rest[:-1]):
            if current[1] in {";", "import", "export"}:
                break
            if current == ("id", "from") and rest[offset + 1][0] == "string":
                imports.append(rest[offset + 1][1])
                break
    return imports[:128]


class _ModuleEntries(HTMLParser):
    """Collect real, active module tags and import maps, including inline JS."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.modules: list[tuple[str, str]] = []
        self.imports: dict[str, str] = {}
        self.scopes: dict[str, dict[str, str]] = {}
        self.inert = 0
        self.script: tuple[str, str] | None = None
        self.body: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"template", "noscript", "textarea"}:
            self.inert += 1
        if tag == "script" and not self.inert and len(self.modules) < 32:
            values = dict(attrs)
            kind = (values.get("type") or "").strip().lower()
            if kind in {"module", "importmap"}:
                self.script = (kind, values.get("src") or "")
                self.body = []

    def handle_data(self, data: str) -> None:
        if self.script:
            self.body.append(data[:_PREFLIGHT_TEXT])

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.script:
            kind, src = self.script
            body = "".join(self.body)[:_PREFLIGHT_TEXT]
            if kind == "module":
                self.modules.append((src, body))
            elif not src:
                try:
                    value = json.loads(body)
                    if isinstance(value, dict):
                        self.imports.update(self._mapping(value.get("imports")))
                        scopes = value.get("scopes", {})
                        if isinstance(scopes, dict):
                            for key, mapping in list(scopes.items())[:64]:
                                if isinstance(key, str) and len(key) <= 4096:
                                    self.scopes[key] = self._mapping(mapping)
                except (ValueError, RecursionError):
                    pass
            self.script, self.body = None, []
        if tag in {"template", "noscript", "textarea"} and self.inert:
            self.inert -= 1

    @staticmethod
    def _mapping(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {key: target for key, target in list(value.items())[:256] if isinstance(key, str) and isinstance(target, str) and target and len(key) <= 4096 and len(target) <= 4096}


def _local_module(root: Path, entry_path: str, src: str, resource_root: str) -> str:
    parsed = urlsplit(src)
    if parsed.scheme or parsed.netloc:
        return ""
    resolved = urlsplit(urljoin("http://preview/" + entry_path, src)).path
    if src.startswith("/") and resource_root:
        resolved = "/" + resource_root + resolved
    clean = _parts(resolved)
    _target(root, clean, must_exist=False)
    return clean


def _metadata_file(root: Path, path: Path) -> str:
    try:
        target = _target(root, path.relative_to(root).as_posix())
        return _text(_read(target, _PREFLIGHT_TEXT))
    except (DesignError, OSError, ValueError):
        return ""


def _package(root: Path, directory: Path) -> dict | None:
    try:
        value = json.loads(_metadata_file(root, directory / "package.json"))
        return value if isinstance(value, dict) else None
    except (ValueError, RecursionError):
        return None


def _static_resource_root(root: Path, entry: Path) -> str:
    independent = _package(root, entry.parent) is not None
    built = entry.parent.name.lower() in {"dist", "build"} and any((entry.parent / name).is_dir() and not (entry.parent / name).is_symlink() for name in ("assets", "static"))
    return entry.parent.relative_to(root).as_posix().removeprefix(".") if independent or built else ""


def _static_vite_port(source: str) -> int | None:
    tokens = _js_tokens(source)
    # Only an explicit server object and numeric port literal. Calls,
    # environment reads, spreads and duplicate keys are never evaluated.
    candidates = []
    for index in range(len(tokens) - 3):
        if tokens[index][1] != "server" or [item[1] for item in tokens[index + 1:index + 3]] != [":", "{"]:
            continue
        depth, ports, uncertain = 1, [], False
        cursor = index + 3
        while cursor < len(tokens) and depth:
            value = tokens[cursor][1]
            if depth == 1 and value == "." and tokens[cursor:cursor + 3] == [("punct", ".")] * 3:
                uncertain = True
            if depth == 1 and value == "port" and cursor + 3 < len(tokens) and tokens[cursor + 1][1] == ":":
                numeric, end = tokens[cursor + 2], tokens[cursor + 3][1]
                if numeric[0] == "number" and numeric[1].isdigit() and end in {",", "}"}:
                    ports.append(int(numeric[1]))
                else:
                    uncertain = True
            if value == "{":
                depth += 1
            elif value == "}":
                depth -= 1
            cursor += 1
        if not uncertain and len(ports) == 1 and 1 <= ports[0] <= 65535:
            candidates.append(ports[0])
    return candidates[0] if len(candidates) == 1 else None


def _object_members(tokens: list[tuple[str, str]]) -> dict[str, list[tuple[str, str]]] | None:
    """Read only unambiguous literal object properties, never execute JS."""
    if not tokens or tokens[0][1] != "{" or tokens[-1][1] != "}":
        return None
    members, cursor = {}, 1
    while cursor < len(tokens) - 1:
        if tokens[cursor][1] == ",":
            cursor += 1
            continue
        if tokens[cursor][0] not in {"id", "string"} or cursor + 1 >= len(tokens) or tokens[cursor + 1][1] != ":":
            return None
        key = tokens[cursor][1]
        if "\\" in key or key in members:
            return None
        cursor += 2
        start, stack = cursor, []
        while cursor < len(tokens) - 1:
            value = tokens[cursor][1]
            punct = tokens[cursor][0] == "punct"
            if punct and not stack and value == ",":
                break
            if punct and value in {"{", "[", "("}:
                stack.append(value)
            elif punct and value in {"}", "]", ")"}:
                if not stack or stack.pop() != {"}": "{", "]": "[", ")": "("}[value]:
                    return None
            cursor += 1
        if stack or cursor == start:
            return None
        members[key] = tokens[start:cursor]
    return members


def _static_vite_api_url(source: str) -> str:
    tokens, candidates = _js_tokens(source), []
    for index in range(len(tokens) - 3):
        if tokens[index][0] not in {"id", "string"} or tokens[index][1] != "server" or [item[1] for item in tokens[index + 1:index + 3]] != [":", "{"]:
            continue
        cursor, depth = index + 2, 0
        while cursor < len(tokens):
            value = tokens[cursor][1]
            if tokens[cursor][0] == "punct" and value == "{":
                depth += 1
            elif tokens[cursor][0] == "punct" and value == "}":
                depth -= 1
                if not depth:
                    break
            cursor += 1
        server = _object_members(tokens[index + 2:cursor + 1])
        proxy = _object_members(server.get("proxy", [])) if server else None
        api = proxy.get("/api", []) if proxy else []
        if len(api) != 1:
            api_object = _object_members(api)
            api = api_object.get("target", []) if api_object else []
        if len(api) == 1 and api[0][0] == "string" and "\\" not in api[0][1]:
            try:
                candidates.append(_loopback_api_url(api[0][1], {8765, 5175}))
            except DesignError:
                pass
    return candidates[0] if len(candidates) == 1 else ""


def suggested_api_url(root: Path, entry: Path) -> str:
    # Prefer the nearest owning frontend configuration. Historical dist
    # artifacts often omit it, so a conventional root/frontend is a bounded
    # fallback. Reading uses the same symlink, hidden and size restrictions.
    directories, directory = [], entry.parent
    for _ in range(12):
        directories.append(directory)
        if directory == root:
            break
        directory = directory.parent
    if root not in directories:
        directories.append(root)
    directories.extend(root / name for name in ("frontend", "web") if root / name not in directories)
    for directory in directories:
        for name in ("vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.mts", "vite.config.cjs", "vite.config.cts"):
            target = _static_vite_api_url(_metadata_file(root, directory / name))
            if target:
                return target
    return ""


def source_runtime(root: Path, entry: Path, data: bytes) -> dict:
    resource_root = _static_resource_root(root, entry)
    relative = entry.relative_to(root).as_posix()
    parser = _ModuleEntries()
    parser.feed(_text(data))
    runtime = {"requires_dev_server": False, "reason": "", "source_entry": "", "working_dir": entry.parent.relative_to(root).as_posix(), "command": "", "suggested_url": ""}
    directory = entry.parent
    for _ in range(12):
        package = _package(root, directory)
        if package is not None:
            runtime["working_dir"] = directory.relative_to(root).as_posix()
            scripts = package.get("scripts")
            if isinstance(scripts, dict) and isinstance(scripts.get("dev"), str) and scripts["dev"].strip():
                runtime["command"] = "npm run dev"
            for name in ("vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.mts", "vite.config.cjs", "vite.config.cts"):
                port = _static_vite_port(_metadata_file(root, directory / name))
                if port and port not in {8765, 5175}:
                    runtime["suggested_url"] = f"http://127.0.0.1:{port}"
                    break
            break
        if directory == root:
            break
        directory = directory.parent

    for src, inline in parser.modules:
        try:
            module = _local_module(root, relative, src, resource_root) if src else relative
        except (DesignError, ValueError):
            continue
        if not module:
            continue
        suffix = Path(urlsplit(src).path).suffix.lower() if src else ""
        reason = ""
        if suffix in _SOURCE_MODULE_SUFFIXES:
            reason = "页面入口使用需要编译的前端源码（TypeScript、JSX 或组件文件）。"
        else:
            source = inline if not src else _metadata_file(root, root / module)
            mappings = dict(parser.imports)
            module_url = "/" + module
            for scope, scope_mapping in sorted(parser.scopes.items(), key=lambda item: len(item[0])):
                scoped_path = urlsplit(urljoin("http://preview/" + relative, scope)).path
                if module_url.startswith(scoped_path):
                    mappings.update(scope_mapping)
            for imported in _module_imports(source):
                imported_path = urlsplit(imported).path
                if Path(imported_path).suffix.lower() in _SOURCE_MODULE_SUFFIXES:
                    reason = "入口脚本直接导入 Vue、Svelte 或 TypeScript/JSX 源码，需要前端开发服务编译。"
                    break
                bare = not imported.startswith((".", "/")) and not urlsplit(imported).scheme
                mapped = imported in mappings or any(key.endswith("/") and imported.startswith(key) and target.endswith("/") for key, target in mappings.items())
                if bare and not mapped:
                    reason = "入口脚本导入未由 import map 解析的依赖包，需要前端开发服务处理。"
                    break
        if reason:
            runtime.update(requires_dev_server=True, reason=reason, source_entry=module)
            break
    return runtime


@dataclass
class PreviewTicket:
    token: str
    user_id: str
    project_id: str
    root: Path
    entry_path: str
    expires: float
    prefix: str
    remote: str = ""
    resource_root: str = ""
    api_target: str = ""
    api_prefix: str = ""


def _resource_url(value: str, base_path: str, ticket: PreviewTicket) -> str:
    value = html.unescape(value).strip()
    if not value or value.startswith(("#", "data:", "blob:")):
        return value
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        if not ticket.remote:
            return "about:blank"
        remote = urlsplit(ticket.remote)
        # localhost and its pinned literal are equivalent only on this ticket.
        hostname = "127.0.0.1" if (parsed.hostname or "").lower() == "localhost" else parsed.hostname
        if parsed.scheme != "http" or hostname != remote.hostname or (parsed.port or 80) != (remote.port or 80) or parsed.username:
            return "about:blank"
        resolved = parsed.path
    else:
        resolved = urlsplit(urljoin("http://preview/" + base_path, value)).path
        if value.startswith("/") and ticket.resource_root and not ticket.remote:
            resolved = "/" + ticket.resource_root + resolved
    try:
        clean = _parts(resolved, live=bool(ticket.remote))
    except DesignError:
        return "about:blank"
    query = "?" + parsed.query if parsed.query else ""
    fragment = "#" + parsed.fragment if parsed.fragment else ""
    directory = "/" if clean and resolved.endswith("/") else ""
    return ticket.prefix + quote(clean, safe="/@:+") + directory + query + fragment


def rewrite_css(text: str, base_path: str, ticket: PreviewTicket) -> str:
    def url(match: re.Match) -> str:
        value = match.group(2).strip()
        return 'url("' + _resource_url(value, base_path, ticket).replace('"', "%22") + '")'
    text = re.sub(r"url\(\s*(['\"]?)(.*?)\1\s*\)", url, text, flags=re.I)
    return re.sub(r"(@import\s+)(['\"])(.*?)\2", lambda m: m[1] + '"' + _resource_url(m[3], base_path, ticket) + '"', text, flags=re.I)


def rewrite_js(text: str, base_path: str, ticket: PreviewTicket) -> str:
    # Only module import/export specifiers and import() are rewritten. Ordinary
    # business strings (including API paths) must not be silently changed.
    identifier = r"[A-Za-z_$][\w$]*"
    named = r"\{[^{}]*\}"
    namespace = rf"\*(?:\s*\bas\s+{identifier})?"
    specifier = rf"(?:{identifier}(?:\s*,\s*(?:{named}|{namespace}))?|{named}|{namespace})\s*\bfrom\b\s*"
    # Named/default/namespace clauses may span multiple lines in real Vue
    # compiler output. Avoid a catch-all scan through unrelated declarations.
    pattern = rf"(\bimport\b\s*(?:{specifier})?|\bexport\b\s*(?:{named}|{namespace})\s*\bfrom\b\s*|\bimport\b\s*\(\s*)(['\"])(/[^'\"\n]+)\2"
    text = re.sub(pattern, lambda m: m[1] + m[2] + _resource_url(m[3], base_path, ticket) + m[2], text)
    # Explicit module-relative asset constructors are resource URLs. Ordinary
    # business strings and fetch/API requests remain untouched.
    constructor = r'(\bnew\s+URL\s*\(\s*)(["\'])(/[^"\'\n]+)\2(\s*,\s*import\.meta\.url\s*\))'
    text = re.sub(constructor, lambda m: m[1] + m[2] + _resource_url(m[3], base_path, ticket) + m[2] + m[4], text)
    # Vite's transformed style module serializes CSS into this explicit
    # variable. Its url() values are resources, not arbitrary business text.
    def vite_css(match: re.Match) -> str:
        try:
            value = json.loads(match[2])
        except (ValueError, TypeError):
            return match[0]
        return match[1] + json.dumps(rewrite_css(value, base_path, ticket), ensure_ascii=False)
    text = re.sub(r'(\b(?:const|let|var)\s+__vite__css\s*=\s*)("(?:\\.|[^"\\])*")', vite_css, text)
    # Imported images/fonts are exposed as URL-only asset modules by Vite.
    asset_export = r'(\bexport\s+default\s*)([\"\'])(/[^\"\'\n]+\.(?:svg|png|jpe?g|gif|webp|avif|ico|woff2?|ttf|otf)(?:\?[^\"\'\n]*)?)\2'
    return re.sub(asset_export, lambda m: m[1] + m[2] + _resource_url(m[3], base_path, ticket) + m[2], text, flags=re.I)


class _StyleLinkPositions(HTMLParser):
    """Locate actual markup without reserializing the user's business source."""
    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.lines = [0, *(match.end() for match in re.finditer("\n", source))]
        self.links: list[tuple[int, int]] = []
        self.head_start: int | None = None
        self.head_end: int | None = None
        self.html_start: int | None = None
        self.doctype_end: int | None = None
        self.body_seen = False
        self.raw_tag = ""
        self.inert_depth = 0

    def source_position(self) -> int:
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.raw_tag:
            return
        if tag in {"textarea", "title", "xmp", "iframe", "noembed", "noframes", "noscript", "plaintext"}:
            self.raw_tag = tag
            return
        if tag in {"template", "svg", "math"}:
            self.inert_depth += 1
        if self.inert_depth:
            return
        if tag == "html" and self.html_start is None:
            self.html_start = self.source_position() + len(self.get_starttag_text() or "")
        if self.head_start is None and tag not in {"html", "head", "meta", "link", "script", "style", "title", "base"}:
            self.body_seen = True
        if tag == "head" and self.head_start is None and not self.body_seen:
            self.head_start = self.source_position() + len(self.get_starttag_text() or "")
        if tag != "link":
            return
        values = dict(attrs)
        if values.get("data-code-agent-design") != "true" and values.get("href") not in {DESIGN_CSS, "./" + DESIGN_CSS}:
            return
        start = self.source_position()
        end = start + len(self.get_starttag_text() or "")
        # Remove the one line break inserted by our own previous save. All
        # other whitespace, comments and scripts keep their original bytes.
        following = self.source[end:]
        if following.startswith("\r\n"):
            end += 2
        elif following.startswith("\n"):
            end += 1
        self.links.append((start, end))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag in {"template", "svg", "math"} and self.inert_depth:
            self.inert_depth -= 1

    def handle_endtag(self, tag: str) -> None:
        if self.raw_tag:
            if tag == self.raw_tag and tag != "plaintext":
                self.raw_tag = ""
            return
        if tag in {"template", "svg", "math"} and self.inert_depth:
            self.inert_depth -= 1
            return
        if not self.inert_depth and tag == "head" and self.head_start is not None and self.head_end is None:
            self.head_end = self.source_position()

    def handle_decl(self, decl: str) -> None:
        if decl.lower().startswith("doctype") and self.doctype_end is None:
            self.doctype_end = self.source_position() + len(decl) + 3


def link_stylesheet(source: str) -> str:
    parser = _StyleLinkPositions(source)
    parser.feed(source)
    newline = "\r\n" if "\r\n" in source else "\n"
    link = f'<link rel="stylesheet" href="./{DESIGN_CSS}" data-code-agent-design="true">' + newline
    position = parser.head_end if parser.head_end is not None else parser.head_start
    if position is None:
        position = 1 if source.startswith("\ufeff") else 0
    operations = [(start, end, "") for start, end in parser.links]
    operations.append((position, position, link))
    output, cursor = [], 0
    for start, end, replacement in sorted(operations):
        output.extend([source[cursor:start], replacement])
        cursor = end
    output.append(source[cursor:])
    return "".join(output)


def preview_policy(ticket: PreviewTicket) -> str:
    connections = ticket.prefix + (" " + ticket.api_prefix if ticket.api_target else "")
    return f"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' {ticket.prefix}; style-src 'unsafe-inline' {ticket.prefix}; img-src data: blob: {ticket.prefix}; font-src data: {ticket.prefix}; media-src blob: {ticket.prefix}; connect-src {connections}; worker-src blob: {ticket.prefix}; form-action 'none'; frame-src 'none'; object-src 'none'"


class _PreviewHtml(HTMLParser):
    def __init__(self, ticket: PreviewTicket, base_path: str):
        super().__init__(convert_charrefs=False)
        self.ticket, self.base_path, self.output = ticket, base_path, []
        self.skip_script = False
        self.import_map = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "base":
            return
        values = dict(attrs)
        if tag == "script":
            self.import_map = (values.get("type") or "").strip().lower() == "importmap"
        if tag == "meta" and (values.get("http-equiv") or "").lower() in {"refresh", "content-security-policy"}:
            return
        if tag == "script" and "/@vite/client" in (values.get("src") or ""):
            self.skip_script = True
            return
        rewritten = []
        for key, value in attrs:
            if value is not None and key in {"src", "href", "poster", "data"}:
                value = _resource_url(value, self.base_path, self.ticket)
            elif value is not None and key == "srcset":
                value = ", ".join(" ".join([_resource_url(bits[0], self.base_path, self.ticket), *bits[1:]]) for item in value.split(",") if (bits := item.strip().split()))
            elif value is not None and key == "style":
                value = rewrite_css(value, self.base_path, self.ticket)
            elif key in {"action", "formaction"}:
                value = "about:blank"
            rewritten.append(key if value is None else f'{key}="{html.escape(value, quote=True)}"')
        self.output.append("<" + tag + (" " + " ".join(rewritten) if rewritten else "") + ">")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.skip_script:
            self.skip_script = False
        else:
            self.output.append(f"</{tag}>")
        if tag == "script":
            self.import_map = False

    def handle_data(self, data: str) -> None:
        if not self.skip_script:
            if self.import_map:
                try:
                    value = json.loads(data)
                    if isinstance(value, dict):
                        mapping = value.get("imports")
                        if isinstance(mapping, dict):
                            value["imports"] = {key: _resource_url(target, self.base_path, self.ticket) if isinstance(target, str) else target for key, target in mapping.items()}
                        scopes = value.get("scopes")
                        if isinstance(scopes, dict):
                            value["scopes"] = {_resource_url(scope, self.base_path, self.ticket): {key: _resource_url(target, self.base_path, self.ticket) if isinstance(target, str) else target for key, target in mapping.items()} if isinstance(mapping, dict) else mapping for scope, mapping in scopes.items()}
                        data = json.dumps(value, ensure_ascii=False)
                except (ValueError, RecursionError):
                    pass
            self.output.append(data)

    def handle_entityref(self, name: str) -> None:
        self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.output.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.output.append("<!--" + data + "-->")

    def handle_decl(self, decl: str) -> None:
        self.output.append("<!" + decl + ">")


def rewrite_html(text: str, base_path: str, ticket: PreviewTicket) -> str:
    parser = _PreviewHtml(ticket, base_path)
    parser.feed(text)
    result = "".join(parser.output)
    # Inline modules/styles also use ticket paths; no HMR websocket is opened.
    result = re.sub(r"(<script\b[^>]*>)(.*?)(</script>)", lambda m: m[1] + rewrite_js(m[2], base_path, ticket) + m[3], result, flags=re.S | re.I)
    result = re.sub(r"(<style\b[^>]*>)(.*?)(</style>)", lambda m: m[1] + rewrite_css(m[2], base_path, ticket) + m[3], result, flags=re.S | re.I)
    directory = base_path.rsplit("/", 1)[0] + "/" if "/" in base_path else ""
    base = '<base href="' + html.escape(ticket.prefix + quote(directory, safe="/"), quote=True) + '">'
    policy = preview_policy(ticket)
    meta = '<meta http-equiv="Content-Security-Policy" content="' + html.escape(policy, quote=True) + '">'
    injection = meta + base
    positions = _StyleLinkPositions(result)
    positions.feed(result)
    if positions.head_start is not None:
        position = positions.head_start
    else:
        # Insert a real head before body markup. Fake <head> text inside a
        # script/comment must never hide the policy in executable source.
        position = positions.html_start or positions.doctype_end or (1 if result.startswith("\ufeff") else 0)
        injection = "<head>" + injection + "</head>"
    return result[:position] + injection + result[position:]


class FrontendDesignService:
    def __init__(self, *, intel: Any = None, clock: Callable[[], float] = time.monotonic, transport: httpx.AsyncBaseTransport | None = None):
        self.intel, self.clock, self.transport = intel, clock, transport
        self.tickets: dict[str, PreviewTicket] = {}
        self._guard = threading.RLock()
        self._locks: dict[str, threading.RLock] = {}

    def _entry(self, project: dict, entry_path: str) -> tuple[Path, Path, bytes]:
        root = Path(project["root_path"]).resolve()
        if entry_path.startswith(("/", "\\")):
            raise DesignError("入口必须使用项目内相对路径")
        if Path(entry_path).suffix.lower() not in {".html", ".htm"}:
            raise DesignError("前端设计入口必须是项目内 HTML 文件")
        entry = _target(root, entry_path)
        data = _read(entry)
        _text(data)
        return root, entry, data

    def stylesheet(self, root: Path, entry: Path) -> dict:
        rel = (entry.parent / DESIGN_CSS).relative_to(root).as_posix()
        target = _target(root, rel, must_exist=False)
        data = _read(target) if target.exists() else b""
        return {"path": rel, "text": _text(data), "sha1": sha1(data) if target.exists() else ""}

    def discover(self, project: dict) -> dict:
        """List entries without reading or preflighting every candidate.

        Build archives may contain dozens of bundled applications. Their source
        and configuration checks belong to preview(), after an entry is chosen;
        discovery must not delay that choice while examining unrelated builds.
        """
        root = Path(project["root_path"]).resolve()
        if not root.is_dir():
            raise DesignError("项目目录已移动或不可用，请重新打开项目", 409)
        entries = []
        visited = 0
        truncated = False
        for directory, dirs, files in os.walk(root, followlinks=False):
            visited += 1
            if visited > MAX_DISCOVERY_DIRECTORIES:
                truncated = True
                break
            dirs[:] = sorted(d for d in dirs if d.lower() not in _DISCOVERY_SKIP and not d.startswith(".") and not (Path(directory) / d).is_symlink())
            for filename in files:
                if filename.lower() == "index.html":
                    path = Path(directory) / filename
                    if path.is_symlink():
                        continue
                    rel = path.relative_to(root).as_posix()
                    # Unknown is distinct from a verified static page. The
                    # selected entry receives the complete runtime/API metadata
                    # from preview(), with all existing validation intact.
                    runtime = {"requires_dev_server": None, "preflight_pending": True,
                               "reason": "选择入口后检查运行方式。", "source_entry": "",
                               "working_dir": path.parent.relative_to(root).as_posix(),
                               "command": "", "suggested_url": ""}
                    entries.append({"path": rel, "label": rel, "runtime": runtime, "api_suggested_url": ""})
            if len(entries) >= MAX_DISCOVERY_ENTRIES:
                truncated = True
                break
        entries = sorted(entries, key=lambda item: (item["path"].count("/"), item["path"]))[:MAX_DISCOVERY_ENTRIES]
        stylesheet = self.stylesheet(root, root / entries[0]["path"]) if entries else {"path": "", "text": "", "sha1": ""}
        return {"entries": entries, "stylesheet": stylesheet, "truncated": truncated,
                "warnings": ["页面入口发现达到扫描上限，列表可能不完整。"] if truncated else []}

    def _new_ticket(self, project: dict, root: Path, entry_path: str, api_base: str, remote: str = "", resource_root: str = "", api_target: str = "") -> PreviewTicket:
        now = self.clock()
        with self._guard:
            self.tickets = {key: value for key, value in self.tickets.items() if value.expires > now}
            while len(self.tickets) >= MAX_TICKETS:
                self.tickets.pop(next(iter(self.tickets)))
            token = secrets.token_urlsafe(32)
            ticket = PreviewTicket(token, project["user_id"], project["id"], root, entry_path, now + TICKET_TTL, api_base.rstrip("/") + ASSET_ROUTE + token + "/", remote, resource_root, api_target, api_base.rstrip("/") + API_ROUTE + token + "/" if api_target else "")
            self.tickets[token] = ticket
        return ticket

    def ticket(self, token: str) -> PreviewTicket:
        with self._guard:
            ticket = self.tickets.get(token)
            if ticket is None or ticket.expires <= self.clock():
                self.tickets.pop(token, None)
                raise DesignError("预览已过期，请刷新预览", 404)
            return ticket

    async def _fetch(self, url: str, *, accept: str = "*/*") -> tuple[bytes, str]:
        try:
            async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False, timeout=8) as client:
                async with client.stream("GET", url, headers={"Accept": accept}) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        raise DesignError("前端服务资源不可用（不跟随重定向）", 502)
                    if int(response.headers.get("content-length") or 0) > MAX_ASSET:
                        raise DesignError("前端资源超过预览大小限制", 413)
                    output = bytearray()
                    async for chunk in response.aiter_bytes():
                        output.extend(chunk)
                        if len(output) > MAX_ASSET:
                            raise DesignError("前端资源超过预览大小限制", 413)
                    return bytes(output), response.headers.get("content-type", "").split(";", 1)[0]
        except httpx.HTTPError as exc:
            raise DesignError("无法连接项目的前端开发服务，请先在终端启动项目", 502) from exc

    async def preview(self, project: dict, *, entry_path: str, url: str, api_base: str, blocked_ports: set[int] | None = None, api_url: str = "") -> dict:
        root, entry, entry_data = self._entry(project, entry_path)
        runtime = source_runtime(root, entry, entry_data)
        api_target = _loopback_api_url(api_url, blocked_ports or {8765, 5175}) if api_url.strip() else ""
        metadata = {"entry_path": entry_path, "entry_sha1": sha1(entry_data), "stylesheet": self.stylesheet(root, entry), "runtime": runtime, "api_url": api_target, "api_proxy_url": "", "api_suggested_url": suggested_api_url(root, entry)}
        data = entry_data
        remote = _loopback_url(url, blocked_ports or {8765, 5175}) if url.strip() else ""
        if runtime["requires_dev_server"] and not remote:
            return {**metadata, "html": "", "base_url": "", "source_kind": "source"}
        base_path = entry.relative_to(root).as_posix()
        source_kind = "static"
        if remote:
            data, content_type = await self._fetch(remote)
            if content_type not in {"text/html", "application/xhtml+xml"} or len(data) > MAX_TEXT:
                raise DesignError("开发服务地址没有返回可预览的 HTML 页面")
            base_path = urlsplit(remote).path.lstrip("/")
            source_kind = "dev-server"
        ticket = self._new_ticket(project, root, entry_path, api_base, remote, _static_resource_root(root, entry) if not remote else "", api_target)
        rewritten = rewrite_html(_text(data), base_path, ticket)
        directory = base_path.rsplit("/", 1)[0] + "/" if "/" in base_path else ""
        return {**metadata, "html": rewritten, "base_url": ticket.prefix + directory, "source_kind": source_kind, "api_proxy_url": ticket.api_prefix}

    async def project_api(self, ticket: PreviewTicket, raw_path: str, query: str = "", *, method: str = "GET", accept: str = "*/*") -> tuple[bytes, str, int]:
        if not ticket.api_target:
            raise DesignError("尚未配置项目后端接口，请填写接口地址后重新载入", 404)
        if method not in {"GET", "HEAD"}:
            raise DesignError("前端验收接口仅支持 GET/HEAD，不允许写入项目数据", 405)
        rel, query = _api_parts(raw_path), _api_query(query)
        target = urlsplit(ticket.api_target)
        url = urlunsplit((target.scheme, target.netloc, target.path + quote(rel, safe="/"), query, ""))
        # Only a normalized content preference is carried from the iframe.
        # Authorization, cookies, origin and all application session headers
        # are deliberately excluded; this client never reads proxy env vars.
        normalized_accept = "application/json" if "application/json" in accept.lower() else "*/*"
        try:
            async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False, timeout=8) as client:
                async with client.stream(method, url, headers={"Accept": normalized_accept}) as response:
                    if 300 <= response.status_code < 400:
                        raise DesignError("项目接口返回重定向，预览不会跟随，请填写实际接口地址", 502)
                    try:
                        declared = int(response.headers.get("content-length") or 0)
                    except ValueError as exc:
                        raise DesignError("项目接口返回了不合法的响应长度", 502) from exc
                    if declared < 0:
                        raise DesignError("项目接口返回了不合法的响应长度", 502)
                    if declared > MAX_ASSET:
                        raise DesignError("项目接口响应超过 8MB 预览限制", 413)
                    output = bytearray()
                    async for chunk in response.aiter_bytes():
                        output.extend(chunk)
                        if len(output) > MAX_ASSET:
                            raise DesignError("项目接口响应超过 8MB 预览限制", 413)
                    media_type = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
                    if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", media_type):
                        media_type = "application/octet-stream"
                    return bytes(output), media_type, response.status_code
        except httpx.HTTPError as exc:
            raise DesignError("无法连接项目后端接口，请确认后端已启动且接口地址正确", 502) from exc

    async def asset(self, ticket: PreviewTicket, raw_path: str, query: str = "", *, accept: str = "*/*") -> tuple[bytes, str]:
        rel = _parts(raw_path, live=bool(ticket.remote))
        if rel == "@vite/client":
            return _HMR_STUB, "application/javascript"
        suffix = Path(rel).suffix.lower()
        if suffix and suffix not in _ASSET_SUFFIXES:
            raise DesignError("此文件类型不是前端预览资源", 404)
        if ticket.remote:
            remote = urlsplit(ticket.remote)
            url = urlunsplit((remote.scheme, remote.netloc, "/" + quote(rel, safe="/@:+"), query, ""))
            accepts_css = any(part.split(";", 1)[0].strip().lower() == "text/css" for part in accept.split(","))
            imported = any(key == "import" for key, _ in parse_qsl(query, keep_blank_values=True))
            # Vite distinguishes an actual stylesheet link from an ES-module
            # CSS import by Accept. Forward only this normalized content hint,
            # never the caller's credentials or arbitrary request headers.
            remote_accept = "text/css" if suffix == ".css" and accepts_css and not imported else "*/*"
            data, content_type = await self._fetch(url, accept=remote_accept)
            if content_type not in {"text/html", "application/xhtml+xml", "text/css", "text/javascript", "application/javascript", "application/json", "application/wasm", "image/svg+xml", "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/x-icon", "image/vnd.microsoft.icon", "font/woff", "font/woff2", "font/ttf", "font/otf", "application/font-woff", "application/vnd.ms-fontobject", "video/mp4", "video/webm", "audio/mpeg", "audio/ogg", "audio/wav", "application/octet-stream"}:
                raise DesignError("开发服务没有返回前端资源的有效 MIME 类型", 422)
            if (rel.startswith("@id/") or rel in {"@react-refresh", "@vite/env"}) and content_type not in {"text/javascript", "application/javascript"}:
                raise DesignError("开发服务虚拟模块必须返回 JavaScript", 422)
            if "api" in {part.lower() for part in rel.split("/")} and content_type not in {"text/javascript", "application/javascript"}:
                raise DesignError("src/api 预览只接受前端 JavaScript 模块，不转发业务 API 响应", 422)
        else:
            if "node_modules" in rel.split("/"):
                raise DesignError("静态预览不公开依赖目录，请使用开发服务", 404)
            path = _target(ticket.root, rel or ticket.entry_path, must_exist=False)
            if not path.exists() and not suffix:
                path = _target(ticket.root, ticket.entry_path)
                rel = ticket.entry_path
            if not path.is_file():
                raise DesignError("前端资源不存在", 404)
            data = _read(path, MAX_ASSET)
            content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if suffix in {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte"} or content_type in {"application/javascript", "text/javascript"}:
            return rewrite_js(_text(data), rel, ticket).encode(), "application/javascript"
        if suffix == ".css" or content_type == "text/css":
            return rewrite_css(_text(data), rel, ticket).encode(), "text/css"
        if content_type in {"text/html", "application/xhtml+xml"}:
            return rewrite_html(_text(data), rel, ticket).encode(), "text/html"
        return data, content_type

    def save_styles(self, project: dict, *, entry_path: str, css: str, expected_sha1: str, expected_entry_sha1: str) -> dict:
        root, entry, _ = self._entry(project, entry_path)
        rel = (entry.parent / DESIGN_CSS).relative_to(root).as_posix()
        target = _target(root, rel, must_exist=False)
        data = css.encode("utf-8")
        if len(data) > MAX_TEXT or b"\0" in data or "</style" in css.lower():
            raise DesignError("样式需要不超过 2MB 的合法 CSS 文本")
        with self._guard:
            lock = self._locks.setdefault(str(root), threading.RLock())
        with lock:
            before_entry = _read(entry)
            existed = target.exists()
            before_css = _read(target) if existed else b""
            current_css = sha1(before_css) if existed else ""
            if sha1(before_entry) != expected_entry_sha1 or current_css != expected_sha1:
                raise DesignError("入口或样式已被其它编辑更新，请刷新后对比再保存", 409)
            source = link_stylesheet(before_entry.decode("utf-8"))
            after_entry = source.encode("utf-8")
            if len(after_entry) > MAX_TEXT:
                raise DesignError("加入样式链接后入口超过 2MB 限制", 413)
            changes = [(target, before_css, data, existed), (entry, before_entry, after_entry, True)]
            changes = [item for item in changes if item[1] != item[2] or not item[3]]
            temporaries: dict[Path, Path] = {}
            written = []
            try:
                for path, before, after, exists in changes:
                    fd, filename = tempfile.mkstemp(prefix=".frontend-design-", dir=path.parent)
                    temporary = Path(filename)
                    temporaries[path] = temporary
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(after)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if exists:
                        os.chmod(temporary, path.stat().st_mode)
                for path, before, after, exists in changes:
                    if _target(root, path.relative_to(root).as_posix(), must_exist=False) != path or path.exists() != exists or (exists and _read(path) != before):
                        raise DesignError("保存期间文件发生变化，请刷新后重试", 409)
                for path, before, after, exists in changes:
                    # Recheck immediately before each replacement too. A file
                    # edited between the two commits must never be overwritten.
                    if _target(root, path.relative_to(root).as_posix(), must_exist=False) != path or path.exists() != exists or (exists and _read(path) != before):
                        raise DesignError("保存期间文件发生变化，请刷新后重试", 409)
                    for committed_path, committed_before, committed_after, committed_existed in written:
                        if _target(root, committed_path.relative_to(root).as_posix()) != committed_path or _read(committed_path) != committed_after:
                            raise DesignError("保存期间样式发生其它变化，请刷新后对比", 409)
                    os.replace(temporaries[path], path)
                    written.append((path, before, after, exists))
            except Exception as exc:
                failed = []
                for path, before, after, exists in reversed(written):
                    try:
                        if _target(root, path.relative_to(root).as_posix()) != path or _read(path) != after:
                            failed.append(path.name)
                            continue
                        if exists:
                            fd, filename = tempfile.mkstemp(prefix=".frontend-design-rollback-", dir=path.parent)
                            temporary = Path(filename)
                            try:
                                with os.fdopen(fd, "wb") as stream:
                                    stream.write(before)
                                os.replace(temporary, path)
                            finally:
                                temporary.unlink(missing_ok=True)
                        else:
                            path.unlink()
                    except OSError:
                        failed.append(path.name)
                if failed:
                    raise DesignError("保存中断，恢复时检测到其它改动；已保留磁盘现状，请检查 " + ", ".join(failed), 500) from exc
                raise
            finally:
                for temporary in temporaries.values():
                    temporary.unlink(missing_ok=True)
            warning, patch_ids = "", []
            if self.intel is not None and changes:
                for path, before, after, exists in changes:
                    try:
                        record = self.intel.record_patch(workspace=root, rel=path.relative_to(root).as_posix(), session_id="", tool="frontend_design_save", before=_text(before), after=_text(after), note="用户保存前端设计样式")
                        if record.get("id"):
                            patch_ids.append(record["id"])
                    except Exception:
                        warning = "样式已保存，改动台账暂不可用。"
                try:
                    self.intel.request_refresh(project)
                except Exception:
                    warning = (warning + " 索引刷新暂不可用。").strip()
            return {"path": rel, "text": css, "sha1": sha1(data), "entry_sha1": sha1(after_entry), "linked": True, "patch_ids": patch_ids, "warning": warning}

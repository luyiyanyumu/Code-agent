"""Project-scoped frontend/backend sessions over the existing owned IDE runtime.

Discovery and configuration never start processes. Explicit start/restart calls
schedule bounded background work; snapshots remain cheap while servers warm up.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx

from code_agent.config import agent_home
from code_agent.codeintel.runtime import IdeRuntime, RuntimeBusy
from code_agent.codeintel.paths import safe_child

ROLES = ("backend", "frontend")
ACTIVE = {"starting", "running", "degraded"}
KINDS = {"npm", "maven", "gradle", "python", "node", "command"}
_FIELDS = {"name", "frontend", "backend", "frontend_url", "backend_url", "health_url", "api_prefix", "strip_api_prefix", "startup_timeout"}
_ROLE_FIELDS = {"mode", "kind", "cwd", "path", "script", "command", "args", "env"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def local_url(raw: str, name: str = "地址") -> str:
    if not isinstance(raw, str) or not raw or len(raw) > 4096 or re.search(r"[\x00-\x20\x7f]", raw) or "\\" in raw:
        raise ValueError(f"{name}必须是本机 HTTP(S) 地址")
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
        port = parts.port
    except ValueError:
        # localhost is a name; all other hosts must be literal loopback IPs.
        parts = urlsplit(raw)
        host = parts.hostname or ""
        if host.lower() != "localhost":
            raise ValueError(f"{name}只允许 localhost、127.0.0.1 或 ::1") from None
        try:
            port = parts.port
        except ValueError:
            raise ValueError(f"{name}端口不合法") from None
        loopback = True
    if parts.scheme not in {"http", "https"} or not loopback or parts.username is not None or parts.password is not None or parts.fragment:
        raise ValueError(f"{name}必须是无用户信息的本机 HTTP(S) 地址")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{name}端口不合法")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _port_open(url):
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)), timeout=.25):
            return True
    except OSError:
        return False


class _ConfigurationStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    @staticmethod
    def key(project_id, user_id):
        return hashlib.sha256(json.dumps([user_id, project_id], ensure_ascii=False).encode()).hexdigest()

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            if self.path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("配置文件过大")
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if value.get("schemaVersion") != 1 or not isinstance(value.get("configurations"), dict):
                raise ValueError("配置结构不合法")
            return value["configurations"]
        except (OSError, ValueError, AttributeError) as exc:
            raise RuntimeBusy(f"联调配置无法读取：{exc}") from exc

    def get(self, project_id, user_id):
        with self.lock:
            return copy.deepcopy(self._read().get(self.key(project_id, user_id)))

    def put(self, project_id, user_id, config):
        with self.lock:
            values = self._read()
            values[self.key(project_id, user_id)] = config
            data = json.dumps({"schemaVersion": 1, "configurations": values}, ensure_ascii=False, indent=2) + "\n"
            if len(data.encode()) > 2 * 1024 * 1024:
                raise ValueError("联调配置总量达到上限")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    if os.name != "nt":
                        os.chmod(temporary, 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)


@dataclass
class _Session:
    project_id: str
    user_id: str
    config: dict
    id: str = field(default_factory=lambda: "integration_" + uuid.uuid4().hex)
    status: str = "starting"
    phase: str = "starting_backend"
    message: str = "正在启动后端"
    error: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    services: dict = field(default_factory=dict)
    cancel: threading.Event = field(default_factory=threading.Event)
    operation: threading.Lock = field(default_factory=threading.Lock)
    generation: int = 0


class IntegrationRuntime:
    def __init__(self, runtime: IdeRuntime, store_path=None):
        self.runtime = runtime
        self.store = _ConfigurationStore(store_path or agent_home() / "integration" / "configurations.json")
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, str], _Session] = {}
        self._all: list[_Session] = []
        self._threads: set[threading.Thread] = set()
        self._http_local = threading.local()
        self._closed = False

    def owned_project(self, project_id, user_id):
        return self.runtime.owned_project(project_id, user_id)

    def _validate(self, project_id, user_id, raw, *, launch=False):
        root = self.runtime._root(project_id, user_id)
        if not isinstance(raw, dict) or set(raw) - _FIELDS:
            raise ValueError("联调配置包含未知字段")
        config = {"name": "前后端联调", "frontend_url": "http://127.0.0.1:5173/", "backend_url": "http://127.0.0.1:8080/",
                  "health_url": "", "api_prefix": "/api", "strip_api_prefix": False, "startup_timeout": 60, **copy.deepcopy(raw)}
        if not isinstance(config["name"], str) or len(config["name"]) > 200 or "\x00" in config["name"]:
            raise ValueError("联调配置名称不合法")
        for key in ("frontend_url", "backend_url"):
            config[key] = local_url(config[key], key)
        if urlsplit(config["backend_url"]).query:
            raise ValueError("后端地址不能包含查询参数，请在接口路径中填写")
        if config["health_url"]:
            config["health_url"] = local_url(config["health_url"], "health_url")
        elif config["health_url"] != "":
            raise ValueError("health_url 必须是字符串")
        prefix = config["api_prefix"]
        if not isinstance(prefix, str) or not re.fullmatch(r"/(?:[A-Za-z0-9_.~-]+/?)*", prefix) or ".." in prefix.split("/") or len(prefix) > 200:
            raise ValueError("api_prefix 必须是 /api 这样的路径前缀")
        config["api_prefix"] = prefix.rstrip("/") or "/"
        if not isinstance(config["strip_api_prefix"], bool):
            raise ValueError("strip_api_prefix 必须是布尔值")
        timeout = config["startup_timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
            raise ValueError("startup_timeout 必须在 1 到 300 秒之间")
        for role in ROLES:
            value = config.get(role, {})
            if not isinstance(value, dict) or set(value) - _ROLE_FIELDS:
                raise ValueError(f"{role} 服务配置不合法")
            value = {"mode": "attach", "kind": "command", "cwd": "", "path": "", "script": "", "command": "", "args": [], "env": {}, **value}
            if value["mode"] not in {"launch", "attach"} or value["kind"] not in KINDS:
                raise ValueError(f"{role} 的连接方式或运行类型不合法")
            for key, limit in (("cwd",4096),("path",4096),("script",512),("command",8192)):
                if not isinstance(value[key], str) or len(value[key]) > limit or "\x00" in value[key]:
                    raise ValueError(f"{role}.{key} 不合法")
            directory = safe_child(root, value["cwd"])
            if value["path"]:
                safe_child(root, value["path"])
            value["args"], value["env"] = self.runtime.launch_options(value["args"], value["env"])
            if launch and value["mode"] == "launch":
                directory = self.runtime._cwd(root, value["cwd"])
                self.runtime._argv(root, directory, **{key:value[key] for key in ("kind","path","script","command","args")})
            config[role] = value
        return config

    def _candidates(self, project_id, user_id):
        rows = self.runtime.run_configs(project_id, user_id)["configs"]
        frontend, backend = [], []
        for row in rows:
            kind, script = row["kind"], row.get("script", "")
            path = (row.get("manifest", "") + "/" + row.get("cwd", "") + "/" + row.get("path", "")).lower()
            if kind == "npm" and re.match(r"^(dev|start|serve|preview)(:|$)", script):
                frontend.append(row)
                if any(part in path for part in ("backend", "server", "api")):
                    backend.append(row)
            elif kind in {"maven", "gradle"} and script in {"spring-boot:run", "bootRun", "run"}:
                backend.append(row)
            elif kind in {"python", "node"} and Path(row.get("path", "")).stem.lower() in {"app", "main", "server", "api", "manage", "asgi"}:
                backend.append(row)
        return {"frontend": frontend, "backend": backend}

    @staticmethod
    def _default(candidates):
        config = {}
        for role in ROLES:
            row = next((row for row in candidates[role] if row.get("available")), None)
            config[role] = {"mode": "launch", **{key:row[key] for key in ("kind","cwd","path","script") if key in row}} if row else {"mode":"attach"}
        return config

    def configuration(self, project_id, user_id):
        self.owned_project(project_id, user_id)
        candidates = self._candidates(project_id, user_id)
        raw = self.store.get(project_id, user_id)
        return {"config": self._validate(project_id, user_id, raw if raw is not None else self._default(candidates)), "candidates": candidates}

    def save_config(self, project_id, user_id, config):
        value = self._validate(project_id, user_id, config)
        self.store.put(project_id, user_id, value)
        return {"config": copy.deepcopy(value), "candidates": self._candidates(project_id, user_id)}

    def _meta(self, session):
        return {key:copy.deepcopy(getattr(session,key)) for key in ("id","status","phase","message","error","services","created_at","updated_at")}

    def _change(self, session, **values):
        with self._lock:
            if "phase" in values and session.phase != values["phase"]:
                session.generation += 1
            for key,value in values.items():
                setattr(session,key,value)
            session.updated_at = _now()

    def _service(self, session, role, **values):
        with self._lock:
            session.services[role].update(values)
            session.updated_at = _now()

    def snapshot(self, project_id, user_id):
        self.owned_project(project_id, user_id)
        with self._lock:
            session = self._sessions.get((user_id, project_id))
            return {"session": self._meta(session) if session else None}

    def context(self, project_id, user_id, session_id=None):
        self.owned_project(project_id, user_id)
        with self._lock:
            session = self._sessions.get((user_id,project_id))
            if session_id is not None and (not session or session.id != session_id):
                raise KeyError("联调会话已失效")
            if not session or session.status not in ACTIVE or session.cancel.is_set():
                raise RuntimeBusy("联调会话未运行")
            return {"session_id":session.id,"status":session.status,"config":copy.deepcopy(session.config),"services":copy.deepcopy(session.services)}

    def _thread(self, target, *args):
        def run():
            try:
                target(*args)
            finally:
                client = getattr(self._http_local, "client", None)
                if client is not None:
                    client.close()
                    del self._http_local.client
                with self._lock:
                    self._threads.discard(threading.current_thread())
        thread = threading.Thread(target=run, daemon=True, name="ide-integration-"+target.__name__)
        with self._lock:
            self._threads.add(thread)
        thread.start()

    def _new(self, project_id, user_id, config):
        session = _Session(project_id,user_id,copy.deepcopy(config))
        session.services = {role:{"mode":config[role]["mode"],"run_id":None,"pid":None,"status":"pending","url":config[role+"_url"],"name":"后端" if role=="backend" else "前端","error":""} for role in ROLES}
        self._sessions[(user_id,project_id)] = session
        self._all = [old for old in self._all if old.status in ACTIVE or old.status == "stopping"] + [session]
        return session

    def start(self, project_id, user_id, config=None):
        self.owned_project(project_id,user_id)
        with self._lock:
            if self._closed:
                raise RuntimeBusy("联调服务已关闭")
            previous = self._sessions.get((user_id,project_id))
            if previous and previous.status in ACTIVE:
                return {"session":self._meta(previous)}
            if previous and previous.status == "stopping":
                raise RuntimeBusy("联调正在停止，请稍后启动")
        if config is None:
            config = self.store.get(project_id,user_id)
            if config is None:
                config = self._default(self._candidates(project_id,user_id))
        value = self._validate(project_id,user_id,config,launch=True)
        with self._lock:
            if self._closed:
                raise RuntimeBusy("联调服务已关闭")
            previous = self._sessions.get((user_id,project_id))
            if previous and previous.status in ACTIVE:
                return {"session":self._meta(previous)}
            if previous and previous.status == "stopping":
                raise RuntimeBusy("联调正在停止，请稍后启动")
            session = self._new(project_id,user_id,value)
            result = {"session":self._meta(session)}
            self._thread(self._bootstrap,session)
            return result

    def _run_state(self, session, role):
        run_id = session.services[role]["run_id"]
        # Status checks must not copy/replay a megabyte of service logs on each
        # readiness poll. The existing run stream owns log replay and cursors.
        return self.runtime.snapshot(session.project_id,session.user_id,run_id,"run",after_seq=2**63-1) if run_id else None

    def _probe(self, session, role):
        explicit = role == "backend" and bool(session.config["health_url"])
        url = session.config["health_url"] if explicit else session.config[role+"_url"]
        try:
            client = getattr(self._http_local, "client", None)
            if client is None:
                client = self._http_local.client = httpx.Client(timeout=.6,trust_env=False,follow_redirects=False)
            with client.stream("GET",url) as response:
                ok = 200 <= response.status_code < (300 if explicit else 500)
                return ok, f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            return False, type(exc).__name__

    def _launch(self, session, role):
        if session.cancel.is_set():
            return False
        options = session.config[role]
        self._change(session,phase="starting_"+role,message="正在启动"+("后端" if role=="backend" else "前端"))
        self._service(session,role,status="starting",error="")
        if options["mode"] == "launch":
            if _port_open(session.config[role+"_url"]):
                raise RuntimeBusy(f"{role} 地址端口已被占用，请停止原服务或选择“连接已有服务”")
            started = self.runtime.create_run(session.project_id,session.user_id,name=session.config["name"]+" · "+role,
                                              **{key:options[key] for key in ("kind","cwd","path","script","command","args","env")})
            self._service(session,role,run_id=started["id"],pid=started["pid"])
        deadline = time.monotonic()+session.config["startup_timeout"]
        self._change(session,phase="waiting_"+role,message="等待"+("后端" if role=="backend" else "前端")+"就绪")
        last = "未响应"
        while not session.cancel.is_set():
            state = self._run_state(session,role)
            if state and state["status"] != "running":
                raise RuntimeBusy(f"{role} 在就绪前退出（退出码 {state['exit_code']}），请查看该服务日志")
            ready,last = self._probe(session,role)
            if ready:
                if session.cancel.wait(.1):
                    return False
                state = self._run_state(session,role)
                if state and state["status"] != "running":
                    raise RuntimeBusy(f"{role} 在就绪前退出（退出码 {state['exit_code']}），请查看该服务日志")
                self._service(session,role,status="ready",error="",http_status=last)
                return True
            if time.monotonic() >= deadline:
                raise RuntimeBusy(f"{role} 启动等待超时（{last}）；请检查地址、端口和服务日志")
            session.cancel.wait(.15)
        return False

    def _stop_role(self, session, role):
        state = self._run_state(session,role)
        if state and state["status"] == "running":
            self.runtime.stop(session.project_id,session.user_id,state["id"],"run")
        self._service(session,role,status="stopped")

    def _cleanup(self, session):
        errors = []
        for role in reversed(ROLES):
            try:
                self._stop_role(session,role)
            except Exception as exc:
                errors.append(str(exc))
        return "; ".join(errors)

    def _bootstrap(self, session, predecessor=None):
        if predecessor:
            with predecessor.operation:
                self._cleanup(predecessor)
                self._change(predecessor,status="stopped",phase="stopped",message="已停止")
        with session.operation:
            try:
                for role in ROLES:
                    if not self._launch(session,role):
                        return
                self._change(session,status="running",phase="monitoring",message="前后端已就绪",error="")
                self._thread(self._monitor,session)
            except Exception as exc:
                failed_phase = session.phase
                cleanup = self._cleanup(session)
                if not session.cancel.is_set():
                    self._change(session,status="failed",phase=failed_phase,message="联调启动失败",error=str(exc)+(f"；清理失败：{cleanup}" if cleanup else ""))

    def _monitor(self, session):
        while not session.cancel.wait(1):
            with self._lock:
                if session.status not in ACTIVE:
                    return
                if session.phase != "monitoring":
                    continue
                generation = session.generation
            healthy = True
            errors = []
            for role in ROLES:
                if session.cancel.is_set():
                    return
                with self._lock:
                    role_state = session.services[role]["status"]
                if role_state == "stopped":
                    healthy = False
                    continue
                state = self._run_state(session,role)
                if state and state["status"] != "running":
                    ok,reason = False,f"进程已退出（退出码 {state['exit_code']}）"
                else:
                    ok,reason = self._probe(session,role)
                with self._lock:
                    if session.generation != generation or session.phase != "monitoring" or session.cancel.is_set():
                        break
                    self._service(session,role,status="ready" if ok else "unhealthy",error="" if ok else reason)
                healthy = healthy and ok
                if not ok:
                    errors.append(role+": "+reason)
            with self._lock:
                if session.generation == generation and session.phase == "monitoring" and not session.cancel.is_set():
                    self._change(session,status="running" if healthy else "degraded",message="前后端已就绪" if healthy else "部分服务未就绪",error="；".join(errors))

    def _stop(self, session, role):
        with session.operation:
            if role == "all":
                error = self._cleanup(session)
                self._change(session,status="stopped",phase="stopped",message="已停止联调",error=error)
            else:
                self._stop_role(session,role)
                with self._lock:
                    any_active = any(row["status"] != "stopped" for row in session.services.values())
                self._change(session,status="degraded" if any_active else "stopped",phase="monitoring" if any_active else "stopped",message="服务已停止")

    def stop(self, project_id, user_id, role="all"):
        self.owned_project(project_id,user_id)
        if role not in {*ROLES,"all"}:
            raise ValueError("role 必须是 all、frontend 或 backend")
        with self._lock:
            session = self._sessions.get((user_id,project_id))
            if not session or session.status in {"stopped","failed","stopping"}:
                return {"session":self._meta(session) if session else None}
            if role != "all" and session.status == "starting":
                raise RuntimeBusy("正在启动，请整体停止或等待就绪后再停止单项服务")
            if role == "all":
                session.cancel.set()
                self._change(session,status="stopping",phase="stopping",message="正在停止联调")
            else:
                self._service(session,role,status="stopping")
                self._change(session,status="degraded",phase="stopping_"+role,message="正在停止服务")
            result = {"session":self._meta(session)}
            self._thread(self._stop,session,role)
            return result

    def _restart_role(self, session, role):
        with session.operation:
            try:
                self._stop_role(session,role)
                if self._launch(session,role):
                    self._change(session,status="running" if all(row["status"]=="ready" for row in session.services.values()) else "degraded",phase="monitoring",message="服务已重启",error="")
            except Exception as exc:
                self._stop_role(session,role)
                if not session.cancel.is_set():
                    self._change(session,status="degraded",phase="monitoring",message="服务重启失败",error=str(exc))

    def restart(self, project_id, user_id, role="all"):
        self.owned_project(project_id,user_id)
        if role not in {*ROLES,"all"}:
            raise ValueError("role 必须是 all、frontend 或 backend")
        with self._lock:
            if self._closed:
                raise RuntimeBusy("联调服务已关闭")
            previous = self._sessions.get((user_id,project_id))
            if role != "all":
                if not previous or previous.status not in {"running","degraded"} or previous.phase != "monitoring":
                    raise RuntimeBusy("请先启动联调并等待当前操作完成")
                self._change(previous,status="starting",phase="restarting_"+role,message="正在重启服务")
                result = {"session":self._meta(previous)}
                self._thread(self._restart_role,previous,role)
                return result
            if previous and previous.status in {"starting","stopping"}:
                raise RuntimeBusy("当前启动或停止操作尚未完成")
        if previous:
            saved = self.store.get(project_id,user_id)
            config = self._validate(project_id,user_id,saved if saved is not None else previous.config,launch=True)
            with self._lock:
                if self._closed:
                    raise RuntimeBusy("联调服务已关闭")
                if self._sessions.get((user_id,project_id)) is not previous or previous.status in {"starting","stopping"}:
                    raise RuntimeBusy("联调状态已变化，请稍后重试")
                previous.cancel.set()
                self._change(previous,status="stopping",phase="stopping",message="正在重启联调")
                session = self._new(project_id,user_id,config)
                result = {"session":self._meta(session)}
                self._thread(self._bootstrap,session,previous)
                return result
        return self.start(project_id,user_id)

    def close(self):
        with self._lock:
            self._closed = True
            sessions = list(self._all)
            for session in sessions:
                session.cancel.set()
                if session.status in ACTIVE:
                    self._change(session,status="stopping",phase="stopping",message="正在停止联调")
        for session in sessions:
            self._thread(self._stop,session,"all")
        deadline = time.monotonic()+10
        while True:
            with self._lock:
                threads = list(self._threads)
            remaining = deadline-time.monotonic()
            if not threads or remaining <= 0:
                return
            for thread in threads:
                thread.join(max(0,min(.1,deadline-time.monotonic())))

    async def aclose(self):
        await asyncio.to_thread(self.close)

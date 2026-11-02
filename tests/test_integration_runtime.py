"""Real paired services and process cleanup; no user projects or model calls."""
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from code_agent.codeintel.integration_runtime import IntegrationRuntime, local_url
from code_agent.codeintel.runtime import IdeRuntime, RuntimeBusy


SERVER = '''import http.server,json,os,pathlib,sys,time,urllib.request
port=int(sys.argv[1]);role=sys.argv[2]
time.sleep(float(os.environ.get("START_DELAY","0")))
if role=="frontend" and os.environ.get("REQUIRE_BACKEND"):
    with urllib.request.urlopen(os.environ["REQUIRE_BACKEND"]) as r:
        assert r.status==200
pathlib.Path(role+".marker").write_text("started")
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        status=int(os.environ.get("HEALTH_STATUS","200")) if self.path=="/health" else 200
        self.send_response(status);self.send_header("Content-Type","application/json");self.end_headers()
        self.wfile.write(json.dumps({"role":role,"path":self.path}).encode())
    def log_message(self,*args):pass
print("service-starting "+role,flush=True)
http.server.ThreadingHTTPServer(("127.0.0.1",port),Handler).serve_forever()
'''


class Projects:
    def __init__(self, root):
        self.rows = {key:{"id":key,"user_id":"owner","root_path":str(root)} for key in ("p","q")}

    def get(self, key):
        return self.rows.get(key)


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        return sock.getsockname()[1]


def listening(value):
    try:
        with socket.create_connection(("127.0.0.1",value),timeout=.2):
            return True
    except OSError:
        return False


def wait(integration, predicate, timeout=15):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        row=integration.snapshot("p","owner")["session"]
        if predicate(row):
            return row
        time.sleep(.03)
    pytest.fail("Integration fixture timed out: "+str(integration.snapshot("p","owner")))


@pytest.fixture
def service(tmp_path):
    root=tmp_path/"真实联调项目 with spaces"
    root.mkdir()
    (root/"server.py").write_text(SERVER,encoding="utf-8")
    (root/"package.json").write_text(json.dumps({"scripts":{"dev":"node server.js","test":"echo test"}}))
    runtime=IdeRuntime(Projects(root))
    integration=IntegrationRuntime(runtime,store_path=tmp_path/"settings/integration.json")
    first,second=port(),port()
    while first==second:
        second=port()
    config={"name":"真实前后端联调","backend_url":f"http://127.0.0.1:{first}/", "frontend_url":f"http://127.0.0.1:{second}/",
            "health_url":f"http://127.0.0.1:{first}/health","startup_timeout":5,
            "backend":{"mode":"launch","kind":"python","path":"server.py","args":[str(first),"backend"],"env":{"START_DELAY":".15","PRIVATE_FIXTURE_SECRET":"test-not-a-real-secret"}},
            "frontend":{"mode":"launch","kind":"python","path":"server.py","args":[str(second),"frontend"],"env":{"REQUIRE_BACKEND":f"http://127.0.0.1:{first}/health"}}}
    try:
        yield integration,runtime,root,config
    finally:
        integration.close()
        runtime.close()


def test_backend_base_rejects_query_but_frontend_and_health_can_use_it(service):
    integration, runtime, root, config = service
    config["backend_url"] += "?tenant=demo"
    with pytest.raises(ValueError, match="后端地址不能包含查询参数"):
        integration.save_config("p", "owner", config)
    assert not integration.store.path.exists()
    config["backend_url"] = config["backend_url"].split("?")[0]
    config["frontend_url"] += "?mode=demo"
    config["health_url"] += "?ready=1"
    saved = integration.save_config("p", "owner", config)["config"]
    assert saved["frontend_url"].endswith("?mode=demo")
    assert saved["health_url"].endswith("?ready=1")


def test_discovery_save_and_reload_do_not_launch_or_modify_project(service,monkeypatch):
    integration,runtime,root,config=service
    before={p.name:p.read_bytes() for p in root.iterdir()}
    monkeypatch.setattr(runtime,"create_run",lambda *a,**k:pytest.fail("Discovery/save must never launch"))
    value=integration.configuration("p","owner")
    assert value["candidates"]["frontend"][0]["script"]=="dev"
    assert value["candidates"]["backend"][0]["path"]=="server.py"
    assert not integration.store.path.exists()
    saved=integration.save_config("p","owner",config)
    another=IntegrationRuntime(runtime,store_path=integration.store.path)
    assert another.configuration("p","owner")["config"]==saved["config"]
    assert another.snapshot("p","owner")=={"session":None}
    assert {p.name:p.read_bytes() for p in root.iterdir()}==before
    assert runtime.list("p","owner","run")["runs"]==[]


def test_real_backend_ready_before_frontend_logs_and_stop_without_env_leak(service):
    integration,runtime,root,config=service
    started=time.monotonic()
    first=integration.start("p","owner",config)
    assert time.monotonic()-started<1.5
    assert first["session"]["status"]=="starting"
    assert integration.start("p","owner",config)["session"]["id"]==first["session"]["id"]
    state=wait(integration,lambda row:row["status"]=="running")
    assert (root/"backend.marker").exists() and (root/"frontend.marker").exists()
    for role in ("frontend","backend"):
        assert httpx.get(config[role+"_url"],trust_env=False).json()["role"]==role
        record=runtime.snapshot("p","owner",state["services"][role]["run_id"],"run")
        assert "service-starting "+role in "".join(event.get("data","") for event in record["events"])
    assert "test-not-a-real-secret" not in json.dumps(state)
    assert "PRIVATE_FIXTURE_SECRET" not in os.environ
    assert "test-not-a-real-secret" not in json.dumps(runtime.list("p","owner","run"))
    stopped=integration.stop("p","owner")
    assert stopped["session"]["status"]=="stopping"
    with pytest.raises(RuntimeBusy):
        integration.context("p","owner",state["id"])
    wait(integration,lambda row:row["status"]=="stopped")
    assert not listening(int(config["backend_url"].split(":")[2].strip("/")))
    assert not listening(int(config["frontend_url"].split(":")[2].strip("/")))


def test_real_frontend_failure_cleans_its_backend_and_preserves_other_run(service):
    integration,runtime,root,config=service
    (root/"other.py").write_text("import time\nprint('other',flush=True)\ntime.sleep(60)\n")
    other=runtime.create_run("p","owner",kind="python",path="other.py")
    config["frontend"]["args"]=["not-a-port","frontend"]
    integration.start("p","owner",config)
    state=wait(integration,lambda row:row["status"]=="failed")
    assert "frontend" in state["error"] and "退出" in state["error"]
    assert not listening(int(config["backend_url"].split(":")[2].strip("/")))
    assert runtime.snapshot("p","owner",other["id"],"run")["status"]=="running"


def test_real_restart_frontend_and_full_restart_invalidate_old_session(service):
    integration,runtime,root,config=service
    integration.start("p","owner",config)
    first=wait(integration,lambda row:row["status"]=="running")
    integration.restart("p","owner","frontend")
    second=wait(integration,lambda row:row["status"]=="running" and row["services"]["frontend"]["run_id"]!=first["services"]["frontend"]["run_id"])
    assert second["id"]==first["id"] and second["services"]["backend"]["run_id"]==first["services"]["backend"]["run_id"]
    integration.restart("p","owner")
    with pytest.raises(KeyError):
        integration.context("p","owner",first["id"])
    third=wait(integration,lambda row:row["status"]=="running")
    assert third["id"]!=first["id"]
    assert runtime.snapshot("p","owner",second["services"]["backend"]["run_id"],"run")["status"]=="stopped"


def test_real_attach_stop_does_not_kill_external_service(service):
    integration,runtime,root,config=service
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404);self.end_headers()
        def log_message(self,*a):pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        address=f"http://127.0.0.1:{server.server_port}/"
        config.update(backend_url=address,frontend_url=address,health_url="")
        config["backend"]={"mode":"attach"};config["frontend"]={"mode":"attach"}
        integration.start("p","owner",config)
        state=wait(integration,lambda row:row["status"]=="running")
        assert not any(row["run_id"] for row in state["services"].values())
        integration.stop("p","owner","frontend")
        wait(integration,lambda row:row["services"]["frontend"]["status"]=="stopped")
        time.sleep(1.2)  # A monitor cycle must not resurrect a detached role.
        assert integration.snapshot("p","owner")["session"]["services"]["frontend"]["status"]=="stopped"
        integration.stop("p","owner")
        wait(integration,lambda row:row["status"]=="stopped")
        assert listening(server.server_port)
        assert httpx.get(address,trust_env=False).status_code==404
    finally:
        server.shutdown();server.server_close();thread.join(2)


def test_launch_port_conflict_does_not_take_over_or_kill_listener(service):
    integration,runtime,root,config=service
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1",0));foreign.listen()
        config["backend_url"]=f"http://127.0.0.1:{foreign.getsockname()[1]}/"
        integration.start("p","owner",config)
        row=wait(integration,lambda row:row["status"]=="failed")
        assert "端口已被占用" in row["error"]
        assert runtime.list("p","owner","run")["runs"]==[]
        assert foreign.getsockname()[1]>0


def test_explicit_health_requires_2xx_and_failed_start_cleans_process(service):
    integration,runtime,root,config=service
    config["startup_timeout"]=1
    config["backend"]["env"]["HEALTH_STATUS"]="404"
    integration.start("p","owner",config)
    row=wait(integration,lambda row:row["status"]=="failed")
    assert "超时" in row["error"]
    assert not (root/"frontend.marker").exists()
    assert not listening(int(config["backend_url"].split(":")[2].strip("/")))


def test_stop_during_start_is_fast_and_prevents_late_frontend(service):
    integration,runtime,root,config=service
    config["backend"]["env"]["START_DELAY"]="3"
    integration.start("p","owner",config)
    start=time.monotonic()
    integration.stop("p","owner")
    assert time.monotonic()-start<.5
    wait(integration,lambda row:row["status"]=="stopped")
    assert not (root/"frontend.marker").exists()
    assert all(row["status"]!="running" for row in runtime.list("p","owner","run")["runs"])


def test_saved_changes_apply_on_full_restart_not_live_context(service):
    integration,runtime,root,config=service
    integration.start("p","owner",config)
    first=wait(integration,lambda row:row["status"]=="running")
    new_port=port()
    config["frontend_url"]=f"http://127.0.0.1:{new_port}/"
    config["frontend"]["args"][0]=str(new_port)
    integration.save_config("p","owner",config)
    assert integration.context("p","owner")["config"]["frontend_url"]==first["services"]["frontend"]["url"]
    integration.restart("p","owner")
    second=wait(integration,lambda row:row["status"]=="running")
    assert second["services"]["frontend"]["url"]==config["frontend_url"]
    assert httpx.get(config["frontend_url"],trust_env=False).json()["role"]=="frontend"


def test_state_never_rediscovers_and_config_is_isolated(service,monkeypatch):
    integration,runtime,root,config=service
    integration.save_config("p","owner",config)
    assert integration.configuration("q","owner")["config"]["name"]!="真实前后端联调"
    monkeypatch.setattr(runtime,"run_configs",lambda *a:pytest.fail("polling must not rescan"))
    assert integration.snapshot("p","owner")=={"session":None}
    for method,args in ((integration.configuration,()),(integration.snapshot,()),(integration.save_config,(config,)),(integration.start,(config,)),(integration.stop,()),(integration.context,())):
        with pytest.raises(KeyError):
            method("p","other",*args)


@pytest.mark.parametrize("address",["https://example.com/","http://127.0.0.1.evil/","file:///etc/passwd","http://user:pass@localhost/","http://127.0.0.1:0/","http://[::1]:99999/","http://localhost/#secret","http://localhost/\nfoo"])
def test_url_boundary(address):
    with pytest.raises(ValueError):
        local_url(address)


def test_launch_options_are_private_and_structured_arguments_survive(service):
    integration,runtime,root,config=service
    (root/"args.py").write_text("import json,os,sys\nprint(json.dumps({'args':sys.argv[1:],'ok':os.getenv('ONLY_THIS_CHILD')=='yes'},ensure_ascii=False),flush=True)\n")
    created=runtime.create_run("p","owner",kind="python",path="args.py",args=["空格 x","semi;literal","&literal"],env={"ONLY_THIS_CHILD":"yes"})
    assert runtime._record("p","owner",created["id"],"run").finished.wait(10)
    result=runtime.snapshot("p","owner",created["id"],"run")
    text="".join(item.get("data","") for item in result["events"])
    assert json.loads(text)=={"args":["空格 x","semi;literal","&literal"],"ok":True}
    assert "ONLY_THIS_CHILD" not in os.environ and "env" not in result
    for args,env in (("bad",{}),([],{"BAD=KEY":"x"}),([],{"KEY":2}),(["x\x00"],{})):
        with pytest.raises(ValueError):
            runtime.launch_options(args,env)

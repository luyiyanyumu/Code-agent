"""Frontend design API, using temporary repositories and fake HTTP only."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.api.frontend_design_api import MAX_PREVIEW_BODY, MAX_STYLES_BODY, mount_frontend_design_api
from code_agent.codeintel.frontend_design import DESIGN_CSS, MAX_ASSET, MAX_TEXT, TICKET_TTL, DesignError, FrontendDesignService, _StyleLinkPositions, sha1


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / "frontend"
    root.mkdir()
    (root / "index.html").write_text('<!doctype html><html><head><title>Fixture</title><link href="/style.css" rel="stylesheet"></head><body><button id="test">Test</button><script type="module" src="./app.js"></script></body></html>', encoding="utf-8")
    (root / "style.css").write_text('body { color: blue; background: url("./image.png") }', encoding="utf-8")
    (root / "app.js").write_text('import {x} from "/module.js"; export const api="/api/business"; document.querySelector("button").onclick=()=>{document.body.dataset.clicked="yes"}', encoding="utf-8")
    (root / "module.js").write_text('export const x=1;', encoding="utf-8")
    (root / "image.png").write_bytes(b"fixture-png")
    projects = {"p": {"id": "p", "root_path": str(root), "user_id": "owner"}}

    def owned(uid, pid):
        if pid not in projects or projects[pid]["user_id"] != uid:
            raise KeyError("not owned")
        return projects[pid]

    patches, scans, requests = [], [], []
    remote_files = {
        "/": ('<html><head><script type="module" src="/@vite/client"></script></head><body><script type="module" src="/src/main.ts"></script><img src="http://localhost:5198/image.png"></body></html>', "text/html"),
        "/src/main.ts": ('import {createHotContext} from "/@vite/client"; import {x} from "/node_modules/.vite/deps/vue.js?v=123"; import.meta.hot=createHotContext("/src/main.ts"); const api="/api/business";', "application/javascript"),
        "/node_modules/.vite/deps/vue.js": ("export const x=1", "application/javascript"),
        "/@react-refresh": ("export default {injectIntoGlobalHook(){}}", "application/javascript"),
        "/@id/__x00__virtual.js": ('export {x} from "/node_modules/.vite/deps/vue.js"', "application/javascript"),
        "/@id/__x00__plugin-vue:export-helper": ('export default (sfc,props)=>{for(const [k,v] of props)sfc[k]=v;return sfc};', "application/javascript"),
        "/src/Style.vue": ('import {updateStyle} from "/@vite/client";const __vite__css="button { background: url(/image.png) }";updateStyle("fixture",__vite__css);const api="/api/business";', "application/javascript"),
        "/src/logo.png": ('export default "/image.png";', "application/javascript"),
        "/src/Multiline.vue": ('import {\n openBlock,\n createElementBlock\n} from "/node_modules/.vite/deps/vue.js?v=123";\nimport DefaultVue, {\n computed\n} from "/node_modules/.vite/deps/vue.js";\nexport {\n x\n} from "/module.js";\nimport * as Vue from "/node_modules/.vite/deps/vue.js";\nimport _export_sfc from "/@id/__x00__plugin-vue:export-helper";\nconst api="/api/business";', "application/javascript"),
        "/src/Compact.vue": ('import{ref}from"/node_modules/.vite/deps/vue.js?v=123";export{x}from"/module.js";import*as Vue from"/node_modules/.vite/deps/vue.js";const lazy=()=>import("/module.js");const api="/api/business";const importFoo="/business/importFoo";', "application/javascript"),
        "/style.css": ('@import "/more.css";a{background:url(/image.png)}', "text/css"),
        "/image.png": (b"image", "image/png"),
        "/redirect": (b"", "redirect"),
        "/big": (b"", "oversize"),
        "/chunked-big": (b"x" * (MAX_ASSET + 1), "application/javascript"),
        "/not-html": (b"{}", "application/json"),
    }

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "127.0.0.1"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if request.url.path == "/negotiated.css":
            css = 'button{color:rgb(18,52,86);background:url(/image.png)}'
            if request.headers.get("accept") == "text/css":
                return httpx.Response(200, content=css, headers={"content-type": "text/css"})
            return httpx.Response(200, content='import{updateStyle}from"/@vite/client";const __vite__css=' + json.dumps(css) + ';updateStyle("fixture",__vite__css);', headers={"content-type": "application/javascript"})
        data, kind = remote_files.get(request.url.path, (b"missing", "missing"))
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "http://external.example/private"})
        if kind == "oversize":
            return httpx.Response(200, headers={"content-length": str(MAX_ASSET + 1)})
        if kind == "missing":
            return httpx.Response(404, content=data)
        return httpx.Response(200, content=data, headers={"content-type": kind, "set-cookie": "must-not-propagate=secret"})

    intel = SimpleNamespace(record_patch=lambda **kw: patches.append(kw) or {"id": f"patch-{len(patches)}"}, request_refresh=lambda project: scans.append(project["id"]) or {})
    clock = [0.0]
    design = FrontendDesignService(intel=intel, clock=lambda: clock[0], transport=httpx.MockTransport(handler))
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser("owner", "fixture", "user")
    mount_frontend_design_api(app, service=SimpleNamespace(get_owned=owned), design=design)
    with TestClient(app) as client:
        yield SimpleNamespace(root=root, project=projects["p"], projects=projects, client=client, app=app, design=design, clock=clock, patches=patches, scans=scans, requests=requests, remote_files=remote_files)


def preview(fixture, **body):
    response = fixture.client.post("/api/v1/code/projects/p/frontend-design/preview", json={"entry_path": "index.html", **body})
    assert response.status_code == 200, response.text
    return response.json()


def payload(fixture, css="button { color: red; }"):
    result = preview(fixture)
    return {"entry_path": "index.html", "css": css, "expected_sha1": result["stylesheet"]["sha1"], "expected_entry_sha1": result["entry_sha1"]}


def save(fixture, body):
    return fixture.client.put("/api/v1/code/projects/p/frontend-design/styles", json=body)


def test_discover_bounded_ignores_hidden_and_dependencies(fixture):
    for directory in ("web", "node_modules/lib", ".git", ".secrets", "dist"):
        path = fixture.root / directory
        path.mkdir(parents=True)
        (path / "index.html").write_text("<html>fixture</html>")
    result = fixture.client.get("/api/v1/code/projects/p/frontend-design").json()
    assert [item["path"] for item in result["entries"]] == ["index.html", "dist/index.html", "web/index.html"]
    assert result["stylesheet"] == {"path": DESIGN_CSS, "text": "", "sha1": ""}
    assert not fixture.patches and not fixture.scans and not fixture.requests


def test_discover_missing_project_root_is_honest(fixture, tmp_path):
    fixture.projects["p"]["root_path"] = str(tmp_path / "moved")
    assert fixture.client.get("/api/v1/code/projects/p/frontend-design").status_code == 409


def _source_project(fixture, main='import {createApp} from "vue"; import App from "./App.vue"; createApp(App).mount("#app")', script="/src/main.js", package=None, config="export default {server:{port:5174}}"):
    directory = fixture.root / "frontend"
    (directory / "src").mkdir(parents=True)
    (directory / "index.html").write_text(f'<html><body><div id="app"></div><script type="module" src="{script}"></script></body></html>', encoding="utf-8")
    (directory / "src/main.js").write_text(main, encoding="utf-8")
    (directory / "package.json").write_text(json.dumps(package if package is not None else {"scripts": {"dev": "vite --host"}}), encoding="utf-8")
    (directory / "vite.config.js").write_text(config, encoding="utf-8")
    return directory


def test_raw_vue_entry_preflight_explains_runtime_without_blank_iframe(fixture):
    _source_project(fixture)
    result = preview(fixture, entry_path="frontend/index.html")
    assert result["html"] == result["base_url"] == ""
    assert result["source_kind"] == "source"
    assert result["runtime"] == {"requires_dev_server": True, "reason": "入口脚本导入未由 import map 解析的依赖包，需要前端开发服务处理。", "source_entry": "frontend/src/main.js", "working_dir": "frontend", "command": "npm run dev", "suggested_url": "http://127.0.0.1:5174"}
    assert result["entry_path"] == "frontend/index.html"
    assert result["stylesheet"]["path"] == "frontend/" + DESIGN_CSS
    assert result["entry_sha1"] == sha1((fixture.root / "frontend/index.html").read_bytes())
    entries = fixture.client.get("/api/v1/code/projects/p/frontend-design").json()["entries"]
    discovered = next(item for item in entries if item["path"] == "frontend/index.html")
    assert discovered["runtime"]["preflight_pending"] is True
    assert discovered["runtime"]["requires_dev_server"] is None
    assert not fixture.design.tickets and not fixture.requests and not fixture.patches


def test_discovery_defers_source_and_bundle_reads_until_preview(fixture, monkeypatch):
    import code_agent.codeintel.frontend_design as design_module

    _source_project(fixture, script="/src/main.ts")
    built = fixture.root / "output" / "historical-build" / "dist"
    (built / "assets").mkdir(parents=True)
    (built / "index.html").write_text('<html><script type="module" src="/assets/app.js"></script></html>', encoding="utf-8")
    (built / "assets" / "app.js").write_text('const bundled = "' + "x" * 300_000 + '";', encoding="utf-8")
    plain = fixture.root / "tmp" / "plain"
    plain.mkdir(parents=True)
    (plain / "index.html").write_text("<html>Plain page</html>", encoding="utf-8")

    def unexpected_read(*args, **kwargs):
        raise AssertionError("entry discovery must not read candidate HTML, bundles or runtime configuration")

    checks = {"runtime": 0, "api": 0}
    def runtime_check(*args, **kwargs):
        checks["runtime"] += 1
        raise AssertionError("runtime preflight belongs to the selected preview")
    def api_check(*args, **kwargs):
        checks["api"] += 1
        raise AssertionError("API inference belongs to the selected preview")

    with monkeypatch.context() as guarded:
        guarded.setattr(design_module, "_read", unexpected_read)
        guarded.setattr(design_module, "source_runtime", runtime_check)
        guarded.setattr(design_module, "suggested_api_url", api_check)
        response = fixture.client.get("/api/v1/code/projects/p/frontend-design")
    assert response.status_code == 200, response.text
    info = response.json()
    assert {item["path"] for item in info["entries"]} == {
        "index.html", "frontend/index.html", "output/historical-build/dist/index.html", "tmp/plain/index.html",
    }
    assert all(item["runtime"]["preflight_pending"] and item["runtime"]["requires_dev_server"] is None for item in info["entries"])
    assert info["truncated"] is False and not info["warnings"]
    assert checks == {"runtime": 0, "api": 0}
    assert not fixture.requests and not fixture.design.tickets

    source = preview(fixture, entry_path="frontend/index.html")
    assert source["runtime"]["requires_dev_server"] is True and not source["html"]
    artifact = preview(fixture, entry_path="output/historical-build/dist/index.html")
    assert artifact["runtime"]["requires_dev_server"] is False and artifact["html"]
    assert preview(fixture, entry_path="tmp/plain/index.html")["source_kind"] == "static"


def test_discovery_reports_entry_limit(fixture, monkeypatch):
    import code_agent.codeintel.frontend_design as design_module

    monkeypatch.setattr(design_module, "MAX_DISCOVERY_ENTRIES", 2)
    for directory in ("a", "b"):
        path = fixture.root / directory
        path.mkdir()
        (path / "index.html").write_text("<html>Page</html>", encoding="utf-8")
    info = fixture.client.get("/api/v1/code/projects/p/frontend-design").json()
    assert len(info["entries"]) == 2
    assert info["truncated"] is True and info["warnings"]


@pytest.mark.parametrize("script", ["/src/main.ts", "/src/main.tsx", "/src/main.jsx", "/src/App.vue", "/src/App.svelte"])
def test_preflight_compiler_module_extensions(fixture, script):
    _source_project(fixture, script=script)
    result = preview(fixture, entry_path="frontend/index.html")
    assert result["runtime"]["requires_dev_server"] is True
    assert result["runtime"]["source_entry"] == "frontend" + script
    assert not result["html"]


@pytest.mark.parametrize("main", ['import App from "./App.vue";', 'export {x} from "./types.ts";', 'const lazy = () => import("./App.svelte");', 'import {x} from "unmapped-package";'])
def test_preflight_direct_imports_requiring_transform(fixture, main):
    _source_project(fixture, main=main)
    assert preview(fixture, entry_path="frontend/index.html")["runtime"]["requires_dev_server"] is True


def test_preflight_ignores_comments_strings_templates_regex_and_inert_html(fixture):
    directory = _source_project(fixture, main='''// import App from "./App.vue";
/* import "vue"; */
const prose = 'import "react"';
const template = `import App from "./App.vue"`;
const regex = /import "vue"/;
import {x} from "./plain.js";
console.log(x);''')
    with (directory / "index.html").open("a", encoding="utf-8") as stream:
        stream.write('<!-- <script type="module" src="./fake.ts"></script> --><template><script type="module" src="./fake.vue"></script></template><textarea><script type="module" src="./fake.jsx"></script></textarea>')
    result = preview(fixture, entry_path="frontend/index.html")
    assert result["runtime"]["requires_dev_server"] is False
    assert result["html"]


def test_preflight_importmap_resolves_bare_modules_and_preserves_capability_paths(fixture):
    directory = _source_project(fixture, main='import {x} from "vue"; import y from "lib/sub.js";')
    (directory / "index.html").write_text('<script type="importmap">{"imports":{"vue":"/vendor/vue.js","lib/":"./vendor/lib/"}}</script><script type="module" src="/src/main.js"></script>', encoding="utf-8")
    result = preview(fixture, entry_path="frontend/index.html")
    assert result["runtime"]["requires_dev_server"] is False
    prefix = result["base_url"].removesuffix("frontend/")
    assert prefix + "frontend/vendor/vue.js" in result["html"]
    assert prefix + "frontend/vendor/lib/" in result["html"]


def test_preflight_importmap_scope_is_module_specific(fixture):
    directory = _source_project(fixture, main='import {x} from "vue";')
    html = '<script type="importmap">{"scopes":{"./other/":{"vue":"/vendor/vue.js"}}}</script><script type="module" src="/src/main.js"></script>'
    (directory / "index.html").write_text(html, encoding="utf-8")
    assert preview(fixture, entry_path="frontend/index.html")["runtime"]["requires_dev_server"] is True
    (directory / "index.html").write_text(html.replace("./other/", "./src/"), encoding="utf-8")
    assert preview(fixture, entry_path="frontend/index.html")["runtime"]["requires_dev_server"] is False


def test_preflight_inline_module_but_ordinary_module_does_not_require_server(fixture):
    (fixture.root / "index.html").write_text('<script type="module">import {x} from "vue";</script>', encoding="utf-8")
    assert preview(fixture)["runtime"]["requires_dev_server"] is True
    (fixture.root / "index.html").write_text('<script type="module">import {x} from "./module.js"; document.body.textContent=x;</script>', encoding="utf-8")
    assert preview(fixture)["runtime"]["requires_dev_server"] is False


@pytest.mark.parametrize("config", [
    'export default {server:{port:Number(process.env.PORT)}}',
    'export default {server:{port:5174 + 1}}',
    'export default {server:{port:5174,...process.env}}',
    'export default {server:{port:5174,port:5176}}',
    '// server:{port:5174}\nexport default {server:{}}',
    'export default {note:"server:{port:5174}",server:{}}',
    'throw new Error("must never execute"); export default {server:{port:getPort()}}',
])
def test_vite_nonstatic_port_is_not_evaluated_or_probed(fixture, config):
    _source_project(fixture, config=config)
    result = preview(fixture, entry_path="frontend/index.html")
    assert result["runtime"]["suggested_url"] == ""
    assert not fixture.requests


def test_runtime_without_existing_dev_script_does_not_invent_command(fixture):
    _source_project(fixture, package={"scripts": {"build": "vite build"}})
    assert preview(fixture, entry_path="frontend/index.html")["runtime"]["command"] == ""


def test_source_runtime_with_live_url_uses_transformed_page(fixture):
    _source_project(fixture)
    result = preview(fixture, entry_path="frontend/index.html", url="http://localhost:5198")
    assert result["runtime"]["requires_dev_server"] is True
    assert result["source_kind"] == "dev-server"
    assert result["html"] and result["base_url"]
    assert fixture.requests


@pytest.mark.parametrize("directory", ["frontend/dist", "frontend/build", "frontend"])
def test_nested_frontend_static_absolute_and_relative_asset_paths(fixture, directory):
    root = fixture.root / directory
    (root / "assets").mkdir(parents=True)
    if directory == "frontend":
        (root / "package.json").write_text("{}", encoding="utf-8")
    (root / "index.html").write_text('<link rel="stylesheet" href="/assets/app.css"><script type="module" src="/assets/app.js"></script><img src="./assets/image.png">', encoding="utf-8")
    (root / "assets/app.js").write_text('import {x} from "/assets/chunk.js"; const image=new URL("/assets/image.png",import.meta.url); const api="/api/business";', encoding="utf-8")
    (root / "assets/chunk.js").write_text("export const x=1", encoding="utf-8")
    (root / "assets/app.css").write_text('@import "/assets/more.css";body{background:url(/assets/image.png)}', encoding="utf-8")
    (root / "assets/more.css").write_text("body{color:red}", encoding="utf-8")
    (root / "assets/image.png").write_bytes(b"fixture-image")
    result = preview(fixture, entry_path=directory + "/index.html")
    assert result["runtime"]["requires_dev_server"] is False
    prefix = result["base_url"].removesuffix(directory + "/")
    assert prefix + directory + "/assets/app.js" in result["html"]
    assert prefix + directory + "/assets/image.png" in result["html"]
    assert directory + "/" + directory not in result["html"]
    js = fixture.client.get(prefix + directory + "/assets/app.js")
    assert js.status_code == 200
    assert prefix + directory + "/assets/chunk.js" in js.text
    assert prefix + directory + "/assets/image.png" in js.text
    assert 'const api="/api/business"' in js.text
    css = fixture.client.get(prefix + directory + "/assets/app.css")
    assert css.status_code == 200
    assert prefix + directory + "/assets/more.css" in css.text
    assert prefix + directory + "/assets/image.png" in css.text
    assert fixture.client.get(prefix + directory + "/assets/image.png").content == b"fixture-image"


def test_plain_nested_html_preserves_project_root_semantics(fixture):
    directory = fixture.root / "pages"
    directory.mkdir()
    (directory / "index.html").write_text('<link href="/style.css" rel="stylesheet"><script type="module" src="../app.js"></script>', encoding="utf-8")
    result = preview(fixture, entry_path="pages/index.html")
    prefix = result["base_url"].removesuffix("pages/")
    assert prefix + "style.css" in result["html"]
    assert prefix + "app.js" in result["html"]
    assert prefix + "pages/style.css" not in result["html"]
    assert fixture.client.get(prefix + "style.css").status_code == 200


def test_public_src_api_module_is_distinct_from_backend_api(fixture):
    (fixture.root / "src/api").mkdir(parents=True)
    (fixture.root / "src/api/client.js").write_text('export const list=()=>fetch("/api/items");', encoding="utf-8")
    static = preview(fixture)
    module = fixture.client.get(static["base_url"] + "src/api/client.js")
    assert module.status_code == 200
    assert 'fetch("/api/items")' in module.text
    fixture.remote_files["/src/api/client.js"] = ('export const list=()=>fetch("/api/items");', "application/javascript")
    live = preview(fixture, url="http://localhost:5198")
    assert fixture.client.get(live["base_url"] + "src/api/client.js").status_code == 200
    before = len(fixture.requests)
    for path in ("api/client.js", "src/api/items", "src/api/items.json", "src/api/credentials.js", "src/api/secrets/client.js", "src/api/private-key.js", "src/api/nested/api/client.js"):
        assert fixture.client.get(live["base_url"] + path).status_code == 422
    assert len(fixture.requests) == before


def test_public_src_api_module_rejects_json_business_response(fixture):
    fixture.remote_files["/src/api/client.js"] = ('{"items":[1]}', "application/json")
    live = preview(fixture, url="http://localhost:5198")
    response = fixture.client.get(live["base_url"] + "src/api/client.js")
    assert response.status_code == 422
    assert "JavaScript" in response.json()["detail"]


@pytest.mark.parametrize("method,path,body", [("GET", "/frontend-design", None), ("POST", "/frontend-design/preview", {"entry_path": "index.html"}), ("PUT", "/frontend-design/styles", {"entry_path": "index.html", "css": "", "expected_sha1": "", "expected_entry_sha1": "a" * 40})])
def test_actions_require_owned_project(fixture, method, path, body):
    fixture.app.dependency_overrides[get_current_user] = lambda: CurrentUser("another", "fixture", "user")
    response = fixture.client.request(method, "/api/v1/code/projects/p" + path, json=body)
    assert response.status_code == 404
    assert not fixture.requests and not fixture.patches


def test_preview_rewrites_resources_without_persistent_tokens_or_writes(fixture):
    before = (fixture.root / "index.html").read_bytes()
    result = preview(fixture)
    assert result["source_kind"] == "static"
    assert result["entry_sha1"] == sha1(before)
    assert 'Content-Security-Policy' in result["html"] and 'frame-src &#x27;none&#x27;' in result["html"]
    assert result["base_url"] + "style.css" in result["html"]
    assert "Bearer" not in result["html"] and "token=" not in result["html"]
    assert (fixture.root / "index.html").read_bytes() == before and not (fixture.root / DESIGN_CSS).exists()
    assert not fixture.patches and not fixture.requests


def test_static_asset_cors_and_module_imports_preserve_business_strings(fixture):
    result = preview(fixture)
    response = fixture.client.get(result["base_url"] + "app.js")
    assert response.status_code == 200
    assert result["base_url"] + "module.js" in response.text
    assert 'api="/api/business"' in response.text
    assert response.headers["access-control-allow-origin"] == "*"
    assert "set-cookie" not in response.headers and response.headers["cache-control"] == "no-store"
    assert "connect-src " + result["base_url"] in response.headers["content-security-policy"]
    css = fixture.client.get(result["base_url"] + "style.css")
    assert result["base_url"] + "image.png" in css.text
    head = fixture.client.head(result["base_url"] + "image.png")
    assert head.status_code == 200 and head.content == b"" and head.headers["content-length"] == "11"


@pytest.mark.parametrize("path", [".env", ".git/config", "secret.json", "credentials.json", "cert.pem", "code_agent.db", "@fs/C:/Users/private.txt", "api/v1/sessions", "node_modules/lib.js", "%2e%2e/private.js", "%252e%252e/private.js", "file.txt"])
def test_assets_reject_private_paths(fixture, path):
    result = preview(fixture)
    response = fixture.client.get(result["base_url"] + path)
    assert response.status_code in {404, 422}
    assert not fixture.requests


def test_ticket_expiry_and_project_binding(fixture):
    result = preview(fixture)
    token = urlsplit(result["base_url"]).path.rstrip("/").split("/")[-1]
    assert len(token) >= 40
    assert fixture.client.get(result["base_url"] + "app.js").status_code == 200
    fixture.clock[0] = TICKET_TTL + 1
    assert fixture.client.get(result["base_url"] + "app.js").status_code == 404
    result = preview(fixture)
    fixture.projects["p"]["user_id"] = "another"
    assert fixture.client.get(result["base_url"] + "app.js").status_code == 404


def test_ticket_invalid_after_root_move(fixture, tmp_path):
    result = preview(fixture)
    fixture.projects["p"]["root_path"] = str(tmp_path / "moved")
    assert fixture.client.get(result["base_url"] + "app.js").status_code == 404


@pytest.mark.parametrize("url", ["https://localhost:5198/", "http://example.com:5198/", "http://192.168.1.2:5198/", "http://localhost:8765/", "http://127.0.0.1:5175/", "http://user:password@localhost:5198/", "http://127.0.0.1:5198/#fragment", "http://169.254.169.254/", "http://2130706433:5198/", "http://0x7f000001:5198/", "http://localhost:5198/api/danger", "http://localhost:5198/.env", "http://localhost:5198/@fs/private.html", "http://localhost:5198/%2e%2e/private.html"])
def test_live_url_blocks_ssrf_and_platform_ports(fixture, url):
    response = fixture.client.post("/api/v1/code/projects/p/frontend-design/preview", json={"entry_path": "index.html", "url": url})
    assert response.status_code == 422 and not fixture.requests


def test_live_vite_is_pinned_proxy_and_hmr_does_not_connect(fixture):
    result = preview(fixture, url="http://localhost:5198/")
    assert result["source_kind"] == "dev-server"
    assert '<script type="module" src="' + result["base_url"] + '@vite/client"' not in result["html"]
    assert result["base_url"] + "src/main.ts" in result["html"]
    assert result["base_url"] + "image.png" in result["html"]
    response = fixture.client.get(result["base_url"] + "src/main.ts")
    assert response.status_code == 200 and result["base_url"] + "@vite/client" in response.text
    assert result["base_url"] + "node_modules/.vite/deps/vue.js?v=123" in response.text
    module = fixture.client.get(result["base_url"] + "node_modules/.vite/deps/vue.js?v=123")
    assert module.status_code == 200
    stub = fixture.client.get(result["base_url"] + "@vite/client")
    assert "createHotContext" in stub.text and "WebSocket" not in stub.text
    assert len(fixture.requests) == 3 and fixture.requests[-1].url.query == b"v=123"


def test_live_react_virtual_modules_and_cache_exception_are_narrow(fixture):
    result = preview(fixture, url="http://localhost:5198/")
    assert fixture.client.get(result["base_url"] + "@react-refresh").status_code == 200
    virtual = fixture.client.get(result["base_url"] + "@id/__x00__virtual.js")
    assert virtual.status_code == 200 and result["base_url"] + "node_modules/.vite/deps/vue.js" in virtual.text
    count = len(fixture.requests)
    for path in ("node_modules/.vite/secrets.json", "other/.vite/deps/a.js", ".vite/deps/a.js", "node_modules/.cache/a.js"):
        assert fixture.client.get(result["base_url"] + path).status_code == 422
    assert len(fixture.requests) == count
    fixture.remote_files["/@react-refresh"] = ("fixture private", "text/plain")
    assert fixture.client.get(result["base_url"] + "@react-refresh").status_code == 422


def test_live_vite_compiled_styles_and_asset_exports_rewrite_only_resource_literals(fixture):
    result = preview(fixture, url="http://localhost:5198/")
    compiled = fixture.client.get(result["base_url"] + "src/Style.vue?vue&type=style")
    assert compiled.status_code == 200 and result["base_url"] + "image.png" in compiled.text
    assert 'api="/api/business"' in compiled.text
    exported = fixture.client.get(result["base_url"] + "src/logo.png?import")
    assert exported.status_code == 200 and 'export default "' + result["base_url"] + 'image.png"' in exported.text


def test_real_vue_multiline_named_default_namespace_and_export_specifiers(fixture):
    result = preview(fixture, url="http://localhost:5198/")
    response = fixture.client.get(result["base_url"] + "src/Multiline.vue")
    assert response.status_code == 200
    assert result["base_url"] + "node_modules/.vite/deps/vue.js?v=123" in response.text
    assert response.text.count(result["base_url"] + "node_modules/.vite/deps/vue.js") == 3
    assert result["base_url"] + "module.js" in response.text
    assert result["base_url"] + "@id/__x00__plugin-vue:export-helper" in response.text
    assert 'api="/api/business"' in response.text
    helper = fixture.client.get(result["base_url"] + "@id/__x00__plugin-vue:export-helper")
    assert helper.status_code == 200 and "export default" in helper.text


def test_vite_virtual_colon_exception_never_allows_drive_paths_or_private_files(fixture):
    result = preview(fixture, url="http://localhost:5198/")
    count = len(fixture.requests)
    for path in ("@id/C:private.js", "@id/C:/Windows/file.js", "@id/plugin-vue:export-helper/%2e%2e/%2e%2e/private.js", "@id/plugin-vue:export-helper/child.js", "@fs/C:/Users/private.js", ".git:config"):
        assert fixture.client.get(result["base_url"] + path).status_code in {422, 404}
    assert len(fixture.requests) == count
    fixture.remote_files["/@id/__x00__plugin-vue:export-helper"] = ("fixture private", "text/plain")
    assert fixture.client.get(result["base_url"] + "@id/__x00__plugin-vue:export-helper").status_code == 422


def test_vue_compact_import_export_and_dynamic_import_need_no_whitespace(fixture):
    result = preview(fixture, url="http://localhost:5198/")
    response = fixture.client.get(result["base_url"] + "src/Compact.vue")
    assert response.status_code == 200
    assert 'import{ref}from"' + result["base_url"] + 'node_modules/.vite/deps/vue.js?v=123"' in response.text
    assert 'export{x}from"' + result["base_url"] + 'module.js"' in response.text
    assert 'import*as Vue from"' + result["base_url"] + 'node_modules/.vite/deps/vue.js"' in response.text
    assert 'import("' + result["base_url"] + 'module.js")' in response.text
    assert 'api="/api/business"' in response.text and 'importFoo="/business/importFoo"' in response.text


def test_real_vite_stylesheet_accept_returns_css_and_css_import_remains_js(fixture):
    result = preview(fixture, url="http://localhost:5198/")
    stylesheet = fixture.client.get(result["base_url"] + "negotiated.css", headers={"accept": "text/css,*/*;q=0.1", "authorization": "Bearer never-forward", "cookie": "fixture=private"})
    assert stylesheet.status_code == 200 and stylesheet.headers["content-type"].startswith("text/css")
    assert "rgb(18,52,86)" in stylesheet.text and result["base_url"] + "image.png" in stylesheet.text
    assert fixture.requests[-1].headers["accept"] == "text/css"
    module = fixture.client.get(result["base_url"] + "negotiated.css", headers={"accept": "*/*"})
    assert module.status_code == 200 and module.headers["content-type"].startswith("application/javascript")
    assert result["base_url"] + "@vite/client" in module.text
    assert fixture.requests[-1].headers["accept"] == "*/*"
    explicit_import = fixture.client.get(result["base_url"] + "negotiated.css?import", headers={"accept": "text/css"})
    assert explicit_import.status_code == 200 and explicit_import.headers["content-type"].startswith("application/javascript")
    assert fixture.requests[-1].headers["accept"] == "*/*"


@pytest.mark.parametrize("url,status", [("http://localhost:5198/redirect", 502), ("http://localhost:5198/big", 413), ("http://localhost:5198/chunked-big", 413), ("http://localhost:5198/not-html", 422)])
def test_live_redirects_size_and_html_type(fixture, url, status):
    response = fixture.client.post("/api/v1/code/projects/p/frontend-design/preview", json={"entry_path": "index.html", "url": url})
    assert response.status_code == status
    assert len(fixture.requests) == 1


def test_live_never_forwards_credentials_or_posts(fixture):
    result = preview(fixture, url="http://localhost:5198/")
    asset = fixture.client.get(result["base_url"] + "image.png", headers={"Authorization": "Bearer should-not-forward", "Cookie": "secret=must-not-forward"})
    assert asset.status_code == 200 and "set-cookie" not in asset.headers
    assert fixture.client.post(result["base_url"] + "image.png").status_code == 405
    assert fixture.client.get(result["base_url"] + "api/business").status_code == 422


def test_css_save_creates_link_with_cas_and_existing_patch_index(fixture):
    body = payload(fixture)
    result = save(fixture, body)
    assert result.status_code == 200, result.text
    value = result.json()
    assert value["linked"] and value["path"] == DESIGN_CSS
    assert (fixture.root / DESIGN_CSS).read_text() == body["css"]
    assert (fixture.root / "index.html").read_text().count('data-code-agent-design="true"') == 1
    assert value["sha1"] == sha1(body["css"].encode())
    assert [patch["rel"] for patch in fixture.patches] == [DESIGN_CSS, "index.html"]
    assert all(patch["tool"] == "frontend_design_save" for patch in fixture.patches)
    assert fixture.scans == ["p"]


def test_subdirectory_entry_saves_own_stylesheet_only(fixture):
    directory = fixture.root / "web"
    directory.mkdir()
    entry = directory / "index.html"
    entry.write_text("<html><head></head><body>fixture</body></html>")
    result = preview(fixture, entry_path="web/index.html")
    response = save(fixture, {"entry_path": "web/index.html", "css": "body{color:red}", "expected_sha1": "", "expected_entry_sha1": result["entry_sha1"]})
    assert response.status_code == 200
    assert response.json()["path"] == "web/" + DESIGN_CSS
    assert not (fixture.root / DESIGN_CSS).exists()


@pytest.mark.parametrize("changed", ["entry", "css"])
def test_stale_save_never_changes_either_file(fixture, changed):
    initial = save(fixture, payload(fixture)).json()
    body = {"entry_path": "index.html", "css": "button{color:green}", "expected_sha1": initial["sha1"], "expected_entry_sha1": initial["entry_sha1"]}
    target = fixture.root / ("index.html" if changed == "entry" else DESIGN_CSS)
    target.write_text("external fixture edit")
    before = [(fixture.root / name).read_bytes() for name in ("index.html", DESIGN_CSS)]
    response = save(fixture, body)
    assert response.status_code == 409
    assert before == [(fixture.root / name).read_bytes() for name in ("index.html", DESIGN_CSS)]
    assert len(fixture.patches) == 2


def test_css_create_collision_is_conflict(fixture):
    body = payload(fixture)
    (fixture.root / DESIGN_CSS).write_text("external")
    assert save(fixture, body).status_code == 409
    assert (fixture.root / DESIGN_CSS).read_text() == "external"
    assert not fixture.patches


def test_save_is_idempotent_without_duplicate_links(fixture):
    first = save(fixture, payload(fixture)).json()
    before = (fixture.root / "index.html").read_bytes()
    second = save(fixture, {"entry_path": "index.html", "css": first["text"], "expected_sha1": first["sha1"], "expected_entry_sha1": first["entry_sha1"]})
    assert second.status_code == 200
    assert (fixture.root / "index.html").read_bytes() == before
    assert len(fixture.patches) == 2


def test_link_save_preserves_script_comment_inert_markup_bom_and_crlf(fixture):
    script = '<script>const head="</head>";const link=\'<link href="./code-agent.design.css">\';console.log(head,link);</script>'
    comment = '<!-- </head><link href="./code-agent.design.css"> -->'
    inert = '<template><link href="./code-agent.design.css"></template><textarea><link href="./code-agent.design.css"></textarea>'
    original = '\ufeff<!doctype html>\r\n<html><head>\r\n' + script + '\r\n' + comment + '\r\n</head><body>' + inert + '</body></html>\r\n'
    (fixture.root / "index.html").write_bytes(original.encode("utf-8"))
    result = save(fixture, payload(fixture))
    assert result.status_code == 200, result.text
    saved = (fixture.root / "index.html").read_bytes().decode("utf-8")
    assert saved.startswith("\ufeff") and script in saved and comment in saved and inert in saved
    parser = _StyleLinkPositions(saved)
    parser.feed(saved)
    assert len(parser.links) == 1
    assert saved.replace('<link rel="stylesheet" href="./code-agent.design.css" data-code-agent-design="true">\r\n', '') == original


def test_link_save_deduplicates_real_links_without_rewriting_other_markup(fixture):
    original = '<HTML><HEAD data-x="a">\n<link href="./code-agent.design.css"><link href="code-agent.design.css">\n<link href="business.css" data-x="keep">\n</HEAD><BODY>ok</BODY></HTML>'
    (fixture.root / "index.html").write_text(original)
    response = save(fixture, payload(fixture))
    assert response.status_code == 200
    saved = (fixture.root / "index.html").read_text()
    parser = _StyleLinkPositions(saved)
    parser.feed(saved)
    assert len(parser.links) == 1 and '<link href="business.css" data-x="keep">' in saved
    assert '<HTML><HEAD data-x="a">' in saved and '</HEAD><BODY>ok</BODY></HTML>' in saved


def test_partial_write_failure_rolls_back_css(fixture, monkeypatch):
    body = payload(fixture)
    before = (fixture.root / "index.html").read_bytes()
    replace = os.replace

    def failure(source, target):
        if Path(target).name == "index.html":
            raise OSError("fixture entry commit failed")
        return replace(source, target)
    monkeypatch.setattr("code_agent.codeintel.frontend_design.os.replace", failure)
    response = save(fixture, body)
    assert response.status_code == 500
    assert not (fixture.root / DESIGN_CSS).exists()
    assert (fixture.root / "index.html").read_bytes() == before
    assert not fixture.patches and not list(fixture.root.glob(".frontend-design-*"))


def test_entry_changed_between_commits_is_not_overwritten(fixture, monkeypatch):
    body = payload(fixture)
    replace = os.replace

    def race(source, target):
        result = replace(source, target)
        if Path(target).name == DESIGN_CSS:
            (fixture.root / "index.html").write_text("external concurrent entry edit")
        return result
    monkeypatch.setattr("code_agent.codeintel.frontend_design.os.replace", race)
    response = save(fixture, body)
    assert response.status_code == 409
    assert (fixture.root / "index.html").read_text() == "external concurrent entry edit"
    assert not (fixture.root / DESIGN_CSS).exists() and not fixture.patches


def test_dev_preview_keeps_original_entry_version_when_disk_changes_during_fetch(fixture):
    before = (fixture.root / "index.html").read_bytes()
    transport = fixture.design.transport
    original = transport.handler

    def changing(request):
        (fixture.root / "index.html").write_text("external during remote fetch")
        return original(request)
    transport.handler = changing
    result = preview(fixture, url="http://localhost:5198/")
    assert result["entry_sha1"] == sha1(before)
    response = save(fixture, {"entry_path": "index.html", "css": "body{}", "expected_sha1": "", "expected_entry_sha1": result["entry_sha1"]})
    assert response.status_code == 409 and not (fixture.root / DESIGN_CSS).exists()


def test_saved_css_preview_and_css_rewrites(fixture):
    result = save(fixture, payload(fixture, '@import "./style.css";button{background:url(/image.png)}')).json()
    previewed = preview(fixture)
    assert previewed["stylesheet"]["sha1"] == result["sha1"]
    assert previewed["base_url"] + DESIGN_CSS in previewed["html"]
    response = fixture.client.get(previewed["base_url"] + DESIGN_CSS)
    assert response.status_code == 200 and previewed["base_url"] + "style.css" in response.text


def test_managed_translate_and_resize_css_persists_and_preview_reads_identical_values(fixture):
    declarations = {"width": "160px", "height": "48px", "translate": "12px -8px"}
    rules = [{"selector": "#test", "declarations": declarations}]
    metadata = base64.b64encode(json.dumps({"version": 1, "rules": rules}, separators=(",", ":")).encode()).decode()
    body = "\n".join(f"  {name}: {value} !important;" for name, value in declarations.items())
    css = f"/* code-agent-frontend-design:v1 {metadata} */\n#test {{\n{body}\n}}\n"

    response = save(fixture, payload(fixture, css))
    assert response.status_code == 200, response.text
    saved = response.json()
    assert (fixture.root / DESIGN_CSS).read_text(encoding="utf-8") == css
    assert saved["sha1"] == sha1(css.encode())

    previewed = preview(fixture)
    assert previewed["stylesheet"]["text"] == css
    assert previewed["stylesheet"]["sha1"] == saved["sha1"]
    asset = fixture.client.get(previewed["base_url"] + DESIGN_CSS)
    assert asset.status_code == 200
    assert asset.text == css
    assert (fixture.root / "index.html").read_text(encoding="utf-8").count('data-code-agent-design="true"') == 1
    assert not fixture.requests, "saving and reading local drag/resize styles must not contact a remote service"


def test_rewrite_blocks_external_resource_and_base_redirect(fixture):
    (fixture.root / "index.html").write_text('<html><head><base href="https://evil.example/"><meta http-equiv="refresh" content="0;url=https://evil.example/"></head><body><img src="https://evil.example/a.png"><form action="http://localhost:8765/api"></form></body></html>')
    result = preview(fixture)
    assert "evil.example" not in result["html"] and "localhost:8765" not in result["html"]
    assert result["html"].count("<base ") == 1


@pytest.mark.parametrize("source", ['<html><body><script>const text="<head>";</script><button>Fixture</button></body></html>', '<!doctype html><html><!-- <head> --><body>Fixture</body></html>', '<html><head><script>const text="<head>";</script></head><body>Fixture</body></html>', '<body><script>const text="<head>";</script>Fixture</body>', '<html><body>Fixture</body><head></head></html>'])
def test_csp_and_base_are_actual_head_elements_not_script_comment_text(fixture, source):
    (fixture.root / "index.html").write_text(source)
    result = preview(fixture)
    tags = []
    class Tokens(HTMLParser):
        def handle_starttag(self, tag, attrs):
            tags.append((tag, dict(attrs)))
    parser = Tokens()
    parser.feed(result["html"])
    policies = [(tag, attrs) for tag, attrs in tags if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy"]
    bases = [(tag, attrs) for tag, attrs in tags if tag == "base"]
    assert len(policies) == 1 and len(bases) == 1
    assert bases[0][1]["href"] == result["base_url"]
    assert "connect-src " + result["base_url"] in policies[0][1]["content"]
    assert next(i for i, (tag, _) in enumerate(tags) if tag == "meta") < next(i for i, (tag, _) in enumerate(tags) if tag in {"body", "script"})
    if 'const text="<head>"' in source:
        assert 'const text="<head>"' in result["html"]


@pytest.mark.parametrize("path,limit", [("/preview", MAX_PREVIEW_BODY), ("/styles", MAX_STYLES_BODY)])
@pytest.mark.parametrize("chunked", [False, True])
def test_inbound_bodies_are_bounded_before_decoding_extra_fields(fixture, path, limit, chunked):
    data = json.dumps({"entry_path": "index.html", "large_extra": "x" * (limit + 1)}).encode()
    headers = {"content-type": "application/json"}
    content = data
    if chunked:
        headers["transfer-encoding"] = "chunked"
        content = (data[index:index + 4096] for index in range(0, len(data), 4096))
    method = "POST" if path == "/preview" else "PUT"
    response = fixture.client.request(method, "/api/v1/code/projects/p/frontend-design" + path, content=content, headers=headers)
    assert response.status_code == 413 and not fixture.requests and not fixture.patches
    assert not (fixture.root / DESIGN_CSS).exists()


def test_bounded_body_preserves_extra_forbidden_double_cas_and_openapi(fixture):
    response = fixture.client.post("/api/v1/code/projects/p/frontend-design/preview", json={"entry_path": "index.html", "extra": "fixture"})
    assert response.status_code == 422 and "fixture" not in response.text
    assert save(fixture, {"entry_path": "index.html", "css": "body{}", "expected_sha1": ""}).status_code == 422
    paths = fixture.client.get("/openapi.json").json()["paths"]
    schema = paths["/api/v1/code/projects/{project_id}/frontend-design/styles"]["put"]["requestBody"]["content"]["application/json"]["schema"]
    assert "expected_entry_sha1" in schema["required"] and schema["additionalProperties"] is False


@pytest.mark.parametrize("path,limit", [("/preview", MAX_PREVIEW_BODY), ("/styles", MAX_STYLES_BODY)])
def test_declared_body_overflow_rejected_without_decoding(fixture, path, limit):
    response = fixture.client.request("POST" if path == "/preview" else "PUT", "/api/v1/code/projects/p/frontend-design" + path, content=b"not json", headers={"content-length": str(limit + 1), "content-type": "application/json"})
    assert response.status_code == 413 and not fixture.requests and not fixture.patches


@pytest.mark.parametrize("entry", ["../index.html", ".git/index.html", "app.js", "missing.html", "/index.html"])
def test_entry_must_be_project_html(fixture, entry):
    response = fixture.client.post("/api/v1/code/projects/p/frontend-design/preview", json={"entry_path": entry})
    assert response.status_code in {404, 422}


def test_oversize_static_entry_rejected(fixture):
    (fixture.root / "index.html").write_bytes(b"x" * (MAX_TEXT + 1))
    response = fixture.client.post("/api/v1/code/projects/p/frontend-design/preview", json={"entry_path": "index.html"})
    assert response.status_code == 413


def test_schema_requires_both_cas_versions(fixture):
    response = save(fixture, {"entry_path": "index.html", "css": "body{}"})
    assert response.status_code == 422
    assert not (fixture.root / DESIGN_CSS).exists()


def test_symlink_asset_rejected(fixture, tmp_path):
    outside = tmp_path / "private.js"
    outside.write_text("private")
    try:
        (fixture.root / "linked.js").symlink_to(outside)
    except OSError:
        pytest.skip("host does not permit symbolic links")
    result = preview(fixture)
    assert fixture.client.get(result["base_url"] + "linked.js").status_code == 422


def test_project_api_disabled_until_explicit_configuration(fixture):
    result = preview(fixture)
    assert result["api_url"] == result["api_proxy_url"] == result["api_suggested_url"] == ""
    token = result["base_url"].rstrip("/").split("/")[-1]
    response = fixture.client.get(f"/api/v1/code/frontend-design-api/{token}/dashboard")
    assert response.status_code == 404 and response.headers["access-control-allow-origin"] == "*"
    assert "frontend-design-api" not in result["html"]
    assert not fixture.requests


@pytest.mark.parametrize("api_url", [
    "https://localhost:8080", "http://example.com:8080", "http://192.168.1.1:8080",
    "http://localhost:8765", "http://127.0.0.1:5175", "http://127.0.0.1:80",
    "http://127.0.0.1:0", "\nhttp://127.0.0.1:8080",
    "http://user:password@localhost:8080", "http://127.0.0.1:8080?secret=x",
    "http://127.0.0.1:8080?", "http://127.0.0.1:8080#", "http://127.0.0.1:8080/admin",
    "http://127.0.0.1:8080/api/../private", "http://127.0.0.1:8080/api/%2e%2e/private",
    "http://127.0.0.1:8080/api/%252e%252e/private", "http://127.0.0.1:8080/api/.env",
    "http://127.0.0.1:8080/api/credentials", "http://127.0.0.1:8080/api/secrets/list",
    "http://127.0.0.1:8080/api/keys.pem", "http://127.0.0.1:8080/api%3Fsecret=x",
    "http://127.0.0.1:8080/api/line\nbreak", "http://127.0.0.1:8080\\api",
])
def test_project_api_target_blocks_ssrf_credentials_and_private_paths(fixture, api_url):
    response = fixture.client.post("/api/v1/code/projects/p/frontend-design/preview", json={"entry_path": "index.html", "api_url": api_url})
    assert response.status_code == 422
    assert not fixture.requests and not fixture.design.tickets


@pytest.mark.parametrize("api_url,normalized", [
    ("http://localhost:8080", "http://127.0.0.1:8080/api/"),
    ("http://127.0.0.1:8080/api", "http://127.0.0.1:8080/api/"),
    ("http://[::1]:8080/api/v1/", "http://[::1]:8080/api/v1/"),
])
def test_project_api_configuration_normalizes_and_does_not_probe(fixture, api_url, normalized):
    result = preview(fixture, api_url=api_url)
    assert result["api_url"] == normalized
    assert result["api_proxy_url"].startswith("http://testserver/api/v1/code/frontend-design-api/")
    assert result["api_proxy_url"].endswith("/")
    assert "connect-src " + result["base_url"] + " " + result["api_proxy_url"] in result["html"]
    assert not fixture.requests and not fixture.patches


def test_project_api_current_application_port_is_always_blocked(fixture):
    with pytest.raises(DesignError, match="代码助手"):
        asyncio.run(fixture.design.preview(fixture.project, entry_path="index.html", url="", api_url="http://localhost:8080", api_base="http://localhost:8080", blocked_ports={8080}))
    assert not fixture.requests


def _mock_project_api(fixture, *, status=200, content=b'{"count":2}', headers=None, failure=False):
    requests = []
    def handler(request):
        requests.append(request)
        if failure:
            raise httpx.ConnectError("private raw diagnostic with credentials", request=request)
        return httpx.Response(status, content=content, headers={"content-type": "application/json", "set-cookie": "project-secret=hidden", **(headers or {})})
    fixture.design.transport = httpx.MockTransport(handler)
    return requests


def test_project_api_read_only_preserves_status_and_excludes_all_outer_credentials(fixture):
    requests = _mock_project_api(fixture)
    result = preview(fixture, api_url="http://localhost:8080/api/v1")
    response = fixture.client.get(result["api_proxy_url"] + "dashboard?page=2", headers={"Authorization": "Bearer outer-secret", "Cookie": "session=outer-secret", "Origin": "http://secret.example", "Accept": "application/json"})
    assert response.status_code == 200 and response.json() == {"count": 2}
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox; frame-ancestors 'none'"
    assert "set-cookie" not in response.headers
    request = requests[-1]
    assert str(request.url) == "http://127.0.0.1:8080/api/v1/dashboard?page=2"
    assert request.headers["accept"] == "application/json"
    assert all(name not in request.headers for name in ("authorization", "cookie", "origin"))
    response = fixture.client.head(result["api_proxy_url"] + "dashboard")
    assert response.status_code == 200 and not response.content and requests[-1].method == "HEAD"
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert fixture.client.request(method, result["api_proxy_url"] + "dashboard").status_code == 405
    assert len(requests) == 2 and not fixture.patches
    assert fixture.client.get(result["base_url"] + "api/dashboard").status_code == 422


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_project_api_http_errors_preserve_business_status(fixture, status):
    requests = _mock_project_api(fixture, status=status, content=b'{"message":"project response"}')
    result = preview(fixture, api_url="http://localhost:8080")
    response = fixture.client.get(result["api_proxy_url"] + "dashboard")
    assert response.status_code == status and response.json() == {"message": "project response"}
    assert len(requests) == 1 and "set-cookie" not in response.headers


@pytest.mark.parametrize("path", [
    "%2e%2e/private", "%252e%252e/private", ".env", "%252eenv", "credentials", "secrets/list", "keys.pem",
    "http:%2f%2fother.example/api", "x%5c..%5cprivate", "x%00y", "users?access_token=secret",
    "users?authorization=secret", "users?password=secret", "users?session_id=secret",
])
def test_project_api_paths_queries_cannot_escape_bound_prefix(fixture, path):
    requests = _mock_project_api(fixture)
    result = preview(fixture, api_url="http://localhost:8080/api/v1")
    response = fixture.client.get(result["api_proxy_url"] + path)
    assert response.status_code in {404, 422}, response.text
    assert not requests


@pytest.mark.parametrize("headers,status", [
    ({"Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "Accept, Content-Type"}, 204),
    ({"Access-Control-Request-Method": "POST"}, 405),
    ({"Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "Authorization"}, 405),
])
def test_project_api_options_never_contacts_backend_and_limits_method_headers(fixture, headers, status):
    requests = _mock_project_api(fixture)
    result = preview(fixture, api_url="http://localhost:8080")
    response = fixture.client.options(result["api_proxy_url"] + "dashboard", headers={"Origin": "null", **headers})
    assert response.status_code == status and response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers and not requests


@pytest.mark.parametrize("kind", ["expired", "reassigned", "moved", "deleted"])
def test_project_api_capability_checks_ticket_and_project_on_every_request(fixture, tmp_path, kind):
    requests = _mock_project_api(fixture)
    result = preview(fixture, api_url="http://localhost:8080")
    if kind == "expired":
        fixture.clock[0] = TICKET_TTL + 1
    elif kind == "reassigned":
        fixture.projects["p"]["user_id"] = "other"
    elif kind == "moved":
        fixture.projects["p"]["root_path"] = str(tmp_path)
    else:
        del fixture.projects["p"]
    assert fixture.client.get(result["api_proxy_url"] + "dashboard").status_code == 404
    assert fixture.client.options(result["api_proxy_url"] + "dashboard").status_code == 404
    assert not requests


@pytest.mark.parametrize("content,headers,status", [
    (b"", {"location": "http://external.example/private"}, 302),
    (b"", {"content-length": str(MAX_ASSET + 1)}, 413),
    (b"x" * (MAX_ASSET + 1), {}, 413),
    (b"", {"content-length": "private-invalid"}, 502),
], ids=["redirect", "declared-too-big", "stream-too-big", "invalid-length"])
def test_project_api_does_not_redirect_and_bounds_stream_response(fixture, content, headers, status):
    requests = _mock_project_api(fixture, status=302 if status == 302 else 200, content=content, headers=headers)
    result = preview(fixture, api_url="http://localhost:8080")
    response = fixture.client.get(result["api_proxy_url"] + "dashboard")
    assert response.status_code == (502 if status == 302 else status)
    assert "location" not in response.headers and len(requests) == 1
    assert response.headers["access-control-allow-origin"] == "*"


def test_project_api_unavailable_error_is_short_honest_and_redacted(fixture):
    _mock_project_api(fixture, failure=True)
    result = preview(fixture, api_url="http://localhost:8080")
    response = fixture.client.get(result["api_proxy_url"] + "dashboard")
    assert response.status_code == 502 and "确认后端已启动" in response.json()["detail"]
    assert "private raw" not in response.text and "credentials" not in response.text


def test_project_api_html_response_cannot_be_used_as_executable_document(fixture):
    _mock_project_api(fixture, content=b"<script>fetch('/api/private')</script>", headers={"content-type": "text/html"})
    result = preview(fixture, api_url="http://localhost:8080")
    response = fixture.client.get(result["api_proxy_url"] + "documentation")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    assert "sandbox" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "fetch" in response.text


def test_project_api_suggestion_uses_nearest_vite_and_static_artifact_fallback(fixture):
    directory = _source_project(fixture, config="export default {server:{proxy:{'/api':{target:'http://localhost:8080',changeOrigin:true}}}}")
    result = preview(fixture, entry_path="frontend/index.html")
    assert result["api_suggested_url"] == "http://127.0.0.1:8080/api/"
    assert result["api_url"] == result["api_proxy_url"] == ""
    artifact = fixture.root / "output/build/dist"
    artifact.mkdir(parents=True)
    (artifact / "index.html").write_text("<html>historical</html>", encoding="utf-8")
    result = preview(fixture, entry_path="output/build/dist/index.html")
    assert result["api_suggested_url"] == "http://127.0.0.1:8080/api/"
    (artifact / "vite.config.js").write_text("export default {server:{proxy:{'/api':'http://127.0.0.1:8081/api/v2'}}}", encoding="utf-8")
    assert preview(fixture, entry_path="output/build/dist/index.html")["api_suggested_url"] == "http://127.0.0.1:8081/api/v2/"
    entries = fixture.client.get("/api/v1/code/projects/p/frontend-design").json()["entries"]
    assert all(item["api_suggested_url"] == "" and item["runtime"]["preflight_pending"] for item in entries)
    assert not fixture.requests and not fixture.patches
    assert (directory / "vite.config.js").exists()


@pytest.mark.parametrize("config", [
    "// server:{proxy:{'/api':{target:'http://localhost:8080'}}}\nexport default {}",
    "const doc=\"server:{proxy:{'/api':{target:'http://localhost:8080'}}}\"; export default {}",
    "export default {server:{proxy:{'/api':{target:process.env.BACKEND}}}}",
    "export default {server:{proxy:{'/api':{target:`http://localhost:8080`}}}}",
    "export default {server:{proxy:{'/api':{target:'http://localhost:8080',target:'http://localhost:8081'}}}}",
    "export default {server:{proxy:{'/api':{target:'http://localhost:8080',...privateOptions}}}}",
    "export default {server:{proxy:{'/api':{target:'http://external.example:8080'}}}}",
    "export default {server:{proxy:{'/api':{target:'http://localhost:8765'}}}}",
])
def test_project_api_suggestion_does_not_evaluate_code_comments_or_ambiguous_targets(fixture, config):
    _source_project(fixture, config=config)
    assert preview(fixture, entry_path="frontend/index.html")["api_suggested_url"] == ""
    assert not fixture.requests

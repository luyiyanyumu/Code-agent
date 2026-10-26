"""任意格式附件：上传 → 落盘 uploads/ → prompt 注入附件清单。

安全边界是重点：attachments 的 path 只允许 uploads/<单段文件名>。
模型会拿到这个路径并可能用工具去读——不清洗就等于给攻击者一个
"让 agent 读工作区外任意文件"的入口。
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from code_agent.context.prompt import compose_prompt_with_attachments


# ----------------------------------------------------------------------
# prompt 组装
# ----------------------------------------------------------------------
def test_compose_prompt_without_attachments_is_unchanged() -> None:
    assert compose_prompt_with_attachments("你好", None) == "你好"
    assert compose_prompt_with_attachments("你好", []) == "你好"


def test_compose_prompt_lists_attachments() -> None:
    out = compose_prompt_with_attachments(
        "总结这个文件",
        [{"name": "报告.pdf", "path": "uploads/报告.pdf", "size": 2048}],
    )
    assert out.startswith("总结这个文件")
    assert "uploads/报告.pdf" in out
    assert "2048 字节" in out
    assert "报告.pdf" in out


# ----------------------------------------------------------------------
# _clean_attachments：路径白名单
# ----------------------------------------------------------------------
def _call_clean(body: dict):
    from code_agent.api.app import _clean_attachments

    return _clean_attachments(body)


def test_clean_attachments_accepts_relative_name() -> None:
    out = _call_clean({"attachments": [{"name": "a.png", "path": "a.png", "size": 3}]})
    assert out == [{"name": "a.png", "path": "uploads/a.png", "size": 3}]


def test_clean_attachments_passes_normal_uploads_path() -> None:
    out = _call_clean({"attachments": [{"name": "a.png", "path": "uploads/a.png"}]})
    assert out == [{"name": "a.png", "path": "uploads/a.png"}]


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",                  # 穿越
        "uploads/../db.json",             # 半截穿越（3 段，直接被段数挡住）
        "/etc/passwd",                    # 绝对路径
        "C:/Windows/win.ini",             # Windows 绝对路径
        "workspaces/s_1/uploads/x.txt",   # 想读别的会话
        "uploads/",                       # 空文件名
        "uploads/..",                     # 目录本身
        "",                               # 空
    ],
)
def test_clean_attachments_rejects_escape_paths(path: str) -> None:
    assert _call_clean({"attachments": [{"name": "x", "path": path}]}) is None


def test_clean_attachments_ignores_junk_and_caps_count() -> None:
    items = [{"not": "a dict"}, "str", None] + [
        {"name": f"f{i}.txt", "path": f"uploads/f{i}.txt"} for i in range(15)
    ]
    out = _call_clean({"attachments": items})
    assert out is not None and len(out) == 10   # 上限 10，垃圾项被忽略


def test_clean_attachments_none_when_missing_or_empty() -> None:
    assert _call_clean({}) is None
    assert _call_clean({"attachments": []}) is None
    assert _call_clean({"attachments": "not-a-list"}) is None


# ----------------------------------------------------------------------
# API fixture
# ----------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / "code-agent-home"
    home.mkdir(parents=True)
    monkeypatch.setenv("CODE_AGENT_HOME", str(home))
    for key in ("CODE_AGENT_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    from code_agent.api.app import create_app

    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        yield c


def _login(client: TestClient) -> dict:
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ----------------------------------------------------------------------
# 上传 / 列表
# ----------------------------------------------------------------------
PNG_MAGIC = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()


def test_upload_any_format_binary_file(client, tmp_path) -> None:
    """二进制（PNG）也能传：落盘内容逐字节一致，且在会话工作区的 uploads/ 下。"""
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]

    r = client.post(
        f"/api/v1/sessions/{sid}/files",
        json={"name": "截图.png", "content_b64": PNG_MAGIC},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "截图.png"
    assert body["path"] == "uploads/截图.png"
    assert body["size"] == 72

    ws_root = client.app.state.sessions.workspaces_root / sid
    data = (ws_root / "uploads" / "截图.png").read_bytes()
    assert data == base64.b64decode(PNG_MAGIC)


def test_upload_same_name_gets_dedup_suffix(client) -> None:
    """同名不覆盖：第二次上传加 -1 后缀，两次的文件都在。"""
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    payload = {"name": "doc.txt", "content_b64": base64.b64encode(b"v1").decode()}
    r1 = client.post(f"/api/v1/sessions/{sid}/files", json=payload, headers=h).json()
    r2 = client.post(f"/api/v1/sessions/{sid}/files", json=payload, headers=h).json()
    assert r1["name"] == "doc.txt"
    assert r2["name"] == "doc-1.txt"

    files = client.get(f"/api/v1/sessions/{sid}/files", headers=h).json()
    assert {f["name"] for f in files} == {"doc.txt", "doc-1.txt"}


def test_upload_rejects_bad_base64_and_empty(client) -> None:
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    assert (
        client.post(
            f"/api/v1/sessions/{sid}/files", json={"name": "x.png", "content_b64": "!!!"}, headers=h
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/v1/sessions/{sid}/files", json={"name": "x.png", "content_b64": ""}, headers=h
        ).status_code
        == 422
    )


def test_upload_size_limit(client, monkeypatch) -> None:
    from code_agent.api import app as app_module

    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    monkeypatch.setattr(app_module, "MAX_UPLOAD_BYTES", 8)
    r = client.post(
        f"/api/v1/sessions/{sid}/files",
        json={"name": "big.bin", "content_b64": base64.b64encode(b"x" * 100).decode()},
        headers=h,
    )
    assert r.status_code == 422 and "上限" in r.text


def test_upload_sanitizes_traversal_name(client, tmp_path) -> None:
    """文件名带路径穿越也要被清洗成安全名字（与 artifacts 同一套规则）。"""
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    r = client.post(
        f"/api/v1/sessions/{sid}/files",
        json={"name": "../../evil.png", "content_b64": PNG_MAGIC},
        headers=h,
    )
    assert r.status_code == 200
    assert "/" not in r.json()["name"] and ".." not in r.json()["name"]
    # 真实文件只落在 uploads/ 里，上级目录没有多出东西
    ws_root = client.app.state.sessions.workspaces_root / sid
    assert (ws_root / "uploads").is_dir()
    assert not list(tmp_path.glob("evil.png"))


def test_upload_requires_own_session(client) -> None:
    h = _login(client)
    from code_agent.api.security import hash_password

    client.app.state.users.create("bob5", hash_password("pw123456"))
    bob = {
        "Authorization": "Bearer "
        + client.post(
            "/api/v1/auth/login", json={"username": "bob5", "password": "pw123456"}
        ).json()["token"]
    }
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    r = client.post(
        f"/api/v1/sessions/{sid}/files",
        json={"name": "x.png", "content_b64": PNG_MAGIC},
        headers=bob,
    )
    assert r.status_code in (403, 404)


# ----------------------------------------------------------------------
# prompt 链路：附件随消息进入事件与 prompt
# ----------------------------------------------------------------------
def test_prompt_with_attachments_keeps_display_clean(client) -> None:
    """用户气泡看到原文，附件清单只进模型侧 prompt（由事件 payload 携带附件列表）。"""
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    client.post(
        f"/api/v1/sessions/{sid}/files",
        json={"name": "data.csv", "content_b64": base64.b64encode(b"a,b\n1,2").decode()},
        headers=h,
    )

    r = client.post(
        f"/api/v1/sessions/{sid}/prompt",
        json={
            "text": "统计这个文件",
            "attachments": [{"name": "data.csv", "path": "uploads/data.csv", "size": 8}],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text

    events = client.get(f"/api/v1/sessions/{sid}/events", headers=h).json()
    user_ev = next(e for e in events if e["type"] == "user_message")
    assert user_ev["payload"]["text"] == "统计这个文件"          # 展示用原文
    assert user_ev["payload"]["attachments"][0]["path"] == "uploads/data.csv"

    # 附件清单确实进了发给模型的 prompt（通过轨迹回放不可见，但可从
    # compose 函数与服务端落盘文件共同验证）
    from code_agent.context.prompt import compose_prompt_with_attachments

    assert "uploads/data.csv" in compose_prompt_with_attachments(
        "统计这个文件", [{"name": "data.csv", "path": "uploads/data.csv", "size": 8}]
    )
    assert (client.app.state.sessions.workspaces_root / sid / "uploads" / "data.csv").exists()


def test_prompt_rejects_escape_attachment_path(client) -> None:
    """想借 attachments 让模型读工作区外文件：直接被清洗掉，不会进 prompt。"""
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    r = client.post(
        f"/api/v1/sessions/{sid}/prompt",
        json={
            "text": "看看这个",
            "attachments": [{"name": "db", "path": "../code_agent.db"}],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    events = client.get(f"/api/v1/sessions/{sid}/events", headers=h).json()
    user_ev = next(e for e in events if e["type"] == "user_message")
    assert user_ev["payload"]["attachments"] == []              # 脏路径被整体丢弃


def test_prompt_attachment_payload_shape_is_json_serializable(client) -> None:
    """事件要能过 model_dump 落盘：附件字段混进奇怪类型也不能炸轨迹。"""
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    r = client.post(
        f"/api/v1/sessions/{sid}/prompt",
        json={
            "text": "hi",
            "attachments": [{"name": "a", "path": "uploads/a", "size": "oops", "extra": {"x": 1}}],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    events = client.get(f"/api/v1/sessions/{sid}/events", headers=h).json()
    att = next(e for e in events if e["type"] == "user_message")["payload"]["attachments"]
    assert att[0]["path"] == "uploads/a"
    json.dumps(att)  # 能序列化

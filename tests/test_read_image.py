"""`read_image`（M51，对齐 DSH 的 `read_image`）。

这个功能要挡的是一种最难发现的问题：**模型描述一张它从没收到过的图**。
所以三层都要钉住：

1. **按签名识别**：`report.png` 可能是改名的 JPEG、也可能根本是文本；
2. **默认关图像输入**（`CODE_AGENT_VISION`）——没开时工具必须明说"像素没有发出去"，
   而不是回一句"已读入图片"；
3. **开了之后字节要一模一样地到请求里**：data URL 解出来必须等于磁盘上的字节。
"""

from __future__ import annotations

import asyncio
import base64
import struct
import zlib

import pytest

from code_agent.context.images import (
    MAX_IMAGES_PER_RUN,
    ImageInbox,
    PendingImage,
    data_url,
    image_size,
    sniff_image,
    vision_enabled,
)
from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin.image_tools import ReadImageTool


# ----------------------------------------------------------------------
# 造几张**真**图（合法头部，尺寸正确）
# ----------------------------------------------------------------------
def _png(width: int = 1, height: int = 1) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00\xff" * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _gif(width: int = 3, height: int = 4) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def _jpeg(width: int = 5, height: int = 6) -> bytes:
    # SOI + APP0(最小) + SOF0 + EOI
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00" + b"\x00" * 9
    sof0 = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\x00" * 9
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def _webp_vp8x(width: int = 7, height: int = 8) -> bytes:
    payload = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
    payload += (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", len(payload) + 4) + b"WEBP" + payload


def _ctx(workspace, inbox: ImageInbox | None = None) -> ToolContext:
    extra = {"images": inbox} if inbox is not None else {}
    return ToolContext(session_id="s", workspace=workspace, extra=extra)


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 1. 按签名识别（不看扩展名）
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "data,expected",
    [
        (_png(), "image/png"),
        (_jpeg(), "image/jpeg"),
        (_gif(), "image/gif"),
        (_webp_vp8x(), "image/webp"),
        (b"hello, world\n", None),
        (b"", None),
    ],
)
def test_sniff_by_signature(data: bytes, expected: str | None) -> None:
    assert sniff_image(data) == expected


def test_renamed_text_file_is_not_an_image(tmp_path) -> None:
    """扩展名不算数：`report.png` 里是文本时必须被拒。"""
    fake = tmp_path / "report.png"
    fake.write_text("这不是图片，只是改了名字", encoding="utf-8")
    out = _run(ReadImageTool().execute({"path": "report.png"}, _ctx(tmp_path, ImageInbox())))
    assert "不是受支持的图片" in out
    assert "文件签名" in out


# ----------------------------------------------------------------------
# 2. 尺寸解析（零依赖）
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "data,expected",
    [
        (_png(640, 480), (640, 480)),
        (_gif(3, 4), (3, 4)),
        (_jpeg(5, 6), (5, 6)),
        (_webp_vp8x(7, 8), (7, 8)),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 4, None),   # 头不全 → 不猜
        (b"not an image", None),
    ],
)
def test_image_size(data: bytes, expected) -> None:
    assert image_size(data) == expected


# ----------------------------------------------------------------------
# 3. 开关：没开图像输入时要**明说**
# ----------------------------------------------------------------------
def test_vision_is_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("CODE_AGENT_VISION", raising=False)
    assert vision_enabled() is False


def test_tool_says_the_pixels_were_not_sent_when_vision_is_off(tmp_path, monkeypatch) -> None:
    """**核心断言**：没开图像输入时不许说"已读入图片"。

    说成"已读入"的话，模型会对着一段它从没看到的字节开始描述画面——
    编得越具体，人越难发现它在编。
    """
    monkeypatch.delenv("CODE_AGENT_VISION", raising=False)
    (tmp_path / "shot.png").write_bytes(_png(640, 480))
    inbox = ImageInbox()
    out = _run(ReadImageTool().execute({"path": "shot.png"}, _ctx(tmp_path, inbox)))

    assert "没有发给模型" in out or "没有发出去" in out
    assert "不要当作你看过这张图" in out
    assert "640×480" in out, "文件事实还是要给的"
    assert inbox.count == 0, "没开开关时不该把图放进注入箱"


def test_tool_adds_the_image_when_vision_is_on(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODE_AGENT_VISION", "1")
    (tmp_path / "shot.png").write_bytes(_png(10, 20))
    inbox = ImageInbox()
    out = _run(ReadImageTool().execute({"path": "shot.png"}, _ctx(tmp_path, inbox)))

    assert "下一次模型请求" in out
    assert "10×20" in out
    assert inbox.count == 1


def test_tool_reports_honestly_without_an_inbox(tmp_path) -> None:
    # 文件不存在是**异常**（路径校验在最前），不是"正常结果"
    with pytest.raises(ToolError):
        _run(ReadImageTool().execute({"path": "x.png"}, _ctx(tmp_path, inbox=None)))

    (tmp_path / "x.png").write_bytes(_png())
    out = _run(ReadImageTool().execute({"path": "x.png"}, _ctx(tmp_path, inbox=None)))
    assert "没有图片通道" in out
    assert "不要把它当作你看过这张图" in out


def test_tool_rejects_a_missing_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODE_AGENT_VISION", "1")
    with pytest.raises(ToolError):
        _run(ReadImageTool().execute({"path": "nope.png"}, _ctx(tmp_path, ImageInbox())))


def test_tool_rejects_empty_path(tmp_path) -> None:
    with pytest.raises(ToolError):
        _run(ReadImageTool().execute({"path": "  "}, _ctx(tmp_path, ImageInbox())))


def test_tool_refuses_outside_the_read_boundary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODE_AGENT_VISION", "1")
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(_png())
    with pytest.raises(ToolError):
        _run(ReadImageTool().execute({"path": "../outside.png"}, _ctx(tmp_path, ImageInbox())))


# ----------------------------------------------------------------------
# 4. 上限
# ----------------------------------------------------------------------
def test_oversize_image_is_refused_with_the_reason(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODE_AGENT_VISION", "1")
    big = tmp_path / "big.png"
    big.write_bytes(_png() + b"\x00" * (5 * 1024 * 1024))   # 5 MiB > 4 MiB 上限
    inbox = ImageInbox()
    out = _run(ReadImageTool().execute({"path": "big.png"}, _ctx(tmp_path, inbox)))
    assert "超过单张上限" in out
    assert inbox.count == 0


def test_inbox_caps_the_number_of_images() -> None:
    inbox = ImageInbox()
    item = PendingImage(path="a.png", mime="image/png", width=1, height=1, size=10, data=b"x")
    for _ in range(MAX_IMAGES_PER_RUN):
        assert inbox.add(item) is None
    reason = inbox.add(item)
    assert reason is not None and "上限" in reason


def test_inbox_take_clears() -> None:
    inbox = ImageInbox()
    inbox.add(PendingImage(path="a.png", mime="image/png", width=1, height=1, size=1, data=b"x"))
    assert len(inbox.take()) == 1
    assert inbox.count == 0


# ----------------------------------------------------------------------
# 5. 接进 loop：字节一模一样地到请求里
# ----------------------------------------------------------------------
def _loop_with_image(workspace, home, *, vision: bool):
    """建一个 loop：第一轮调 read_image，第二轮收口。返回 (loop, 每次调用的 messages)。"""
    from code_agent.agent.loop import AgentLoop
    from code_agent.permissions.hooks import PermissionManager
    from code_agent.providers.offline import OfflineProvider
    from code_agent.storage.jsonl import TranscriptStore
    from code_agent.tools.registry import ToolRegistry

    seen: list[list[dict]] = []

    class Spy(OfflineProvider):
        async def chat(self, messages, tools):
            seen.append([dict(m) for m in messages])
            return await super().chat(messages, tools)

    reg = ToolRegistry()
    reg.register(ReadImageTool())
    loop = AgentLoop(
        provider=Spy(
            script=[
                OfflineProvider.tool_call_turn("read_image", {"path": "shot.png", "note": "看下报错"}),
                OfflineProvider.text_turn("看到了"),
            ]
        ),
        registry=reg,
        permissions=PermissionManager(workspace_root=workspace),
        transcript=TranscriptStore(home / "transcripts"),
        workspace=workspace,
    )
    return loop, seen


def _image_blocks(messages: list[dict]) -> list[dict]:
    out = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            out += [b for b in content if isinstance(b, dict) and b.get("type") == "image_url"]
    return out


def test_loop_attaches_the_exact_bytes_to_the_next_request(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODE_AGENT_VISION", "1")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    payload = _png(64, 32)
    (workspace / "shot.png").write_bytes(payload)

    loop, seen = _loop_with_image(workspace, tmp_path, vision=True)
    _run(loop.run("看下这张图"))

    assert len(seen) == 2
    assert _image_blocks(seen[0]) == [], "第一次请求不该带图"
    blocks = _image_blocks(seen[1])
    assert len(blocks) == 1, "第二次请求必须带上图片块"

    url = blocks[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == payload, "发出去的字节必须与磁盘一致"

    # 说明文字也要在（模型要知道这是外部输入而不是指令）
    texts = [
        b["text"] for m in seen[1] if isinstance(m.get("content"), list)
        for b in m["content"] if isinstance(b, dict) and b.get("type") == "text"
    ]
    assert any("read_image" in t and "不能当指令" in t for t in texts), texts
    assert any("看下报错" in t for t in texts), "note 要带上"


def test_loop_does_not_attach_images_when_vision_is_off(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CODE_AGENT_VISION", raising=False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "shot.png").write_bytes(_png())

    loop, seen = _loop_with_image(workspace, tmp_path, vision=False)
    _run(loop.run("看下这张图"))

    assert _image_blocks(seen[1]) == [], "没开图像输入却把图发了出去"

    # 工具结果里必须已经有"像素没发出去"的说明
    results = [
        e.payload["result"] for e in loop.transcript.replay(loop.session_id)
        if e.type.value == "tool_result"
    ]
    assert any("没有发给模型" in r for r in results), results


def test_attached_image_is_recorded_as_a_quiet_event(tmp_path, monkeypatch) -> None:
    """附了图要留痕（刷新后仍在、界面渲染成一行淡字），否则"AI 看到了什么"不可回放。"""
    monkeypatch.setenv("CODE_AGENT_VISION", "1")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "shot.png").write_bytes(_png())

    loop, _seen = _loop_with_image(workspace, tmp_path, vision=True)
    _run(loop.run("看下这张图"))

    events = [
        e for e in loop.transcript.replay(loop.session_id)
        if e.type.value == "user_message" and e.payload.get("kind") == "image"
    ]
    assert len(events) == 1
    assert events[0].payload["quiet"] is True
    assert "shot.png" in events[0].payload["text"]


def test_data_url_roundtrip() -> None:
    url = data_url("image/png", b"\x00\x01\x02")
    assert url == "data:image/png;base64," + base64.b64encode(b"\x00\x01\x02").decode()

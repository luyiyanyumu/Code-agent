"""图片读取与注入（M51，对齐 DSH 的 `read_image`）。

模型看不到图这一个缺口很具体：设计稿、报错截图、用户拍的票据、图表 PNG——以前
只能靠 `bash` 把文件 base64 一遍或者干脆"读不了"。这里把它变成一条正规通道：
`read_image` 读进来 → 随**下一次模型请求**作为图片块发出去。

四条设计约束，每条都对应一个真实的坑：

1. **按文件签名识别，不看扩展名**。`report.png` 完全可能是改名的 JPEG，也可能是
   纯文本；只看扩展名会把后者当成图片发给模型（发出去的是垃圾，而模型以为看到了图）。
2. **尺寸自己解析，不引入依赖**。PNG IHDR / JPEG SOF / GIF / WebP 的头都在几十字节内，
   零依赖解析比"装个 Pillow"更符合这个项目的取舍（venv 里连 openpyxl 都没有）。
   尺寸是给模型与人的一句人话（"1024×768"），也是"这图是不是缩略图"的判断依据。
3. **有上限，超了如实拒绝**。图片进上下文是要花 token 的（而且是按块算的大头），
   一次塞十张 10MB 的图会把一轮请求撑爆。默认单张 ≤ 4 MiB、一轮 ≤ 3 张。
4. **没有图像能力就说没有**。`CODE_AGENT_VISION=0`（默认见下）时**不假装**读到了图：
   工具仍然给出签名/尺寸/大小（这些是有用的事实），并明确说明"图片字节没有发给模型"。
   默认关是因为 code-agent 的默认 provider 是 deepseek，其 chat 接口不吃 `image_url` 块——
   默认开着会让每次读图都变成一次 provider 报错。
"""

from __future__ import annotations

import base64
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

#: 单张图片字节上限（超过就拒绝，并如实说明为什么）。
MAX_IMAGE_BYTES = 4 * 1024 * 1024
#: 一次请求里最多附几张（多图很吃 token，而且模型通常也只需要关键那一两张）。
MAX_IMAGES_PER_RUN = 3

#: 环境变量：设为 "1" 才把图片字节发给模型。默认关（见模块头的第 4 条）。
VISION_ENV = "CODE_AGENT_VISION"


def vision_enabled() -> bool:
    """本部署是否把图片发给模型。默认 **关**。"""
    return (os.environ.get(VISION_ENV) or "").strip() in ("1", "true", "yes", "on")


# ----------------------------------------------------------------------
# 签名与尺寸（零依赖）
# ----------------------------------------------------------------------
def sniff_image(data: bytes) -> str | None:
    """按**文件签名**判断图片类型，返回 mime；不是支持的图片则 None。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _png_size(data: bytes) -> tuple[int, int] | None:
    # 8 字节签名 + 4 字节长度 + "IHDR" + 宽(4) + 高(4)
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10:
        return None
    return struct.unpack("<HH", data[6:10])


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """扫 JPEG 的段，找 SOF0/1/2/…（0xFFC0–0xFFCF，跳过 C4/C8/CC 那几个非 SOF）。"""
    i = 2
    end = len(data)
    while i + 9 < end:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > end:
            return None
        seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 > end:
                return None
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + seg_len
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 " and len(data) >= 30:
        # 关键帧头：3 字节起始码 + 2 字节宽 + 2 字节高（各 14 位有效）
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


def image_size(data: bytes, mime: str | None = None) -> tuple[int, int] | None:
    """图片的 (宽, 高)；解析不出来返回 None（**不猜**）。"""
    kind = mime or sniff_image(data)
    if kind == "image/png":
        return _png_size(data)
    if kind == "image/gif":
        return _gif_size(data)
    if kind == "image/jpeg":
        return _jpeg_size(data)
    if kind == "image/webp":
        return _webp_size(data)
    return None


# ----------------------------------------------------------------------
# 一轮请求里待注入的图片
# ----------------------------------------------------------------------
@dataclass
class PendingImage:
    path: str
    mime: str
    width: int | None
    height: int | None
    size: int
    note: str = ""
    #: **字节一起带着**：注入时不要再按路径重读一遍——既多一次 I/O，又给了
    #: "读的时候是这张图、注入的时候换成另一张"的窗口（TOCTOU）。
    #: 上限是 3 张 × 4 MiB，注入后立刻释放。
    data: bytes = b""

    def describe(self) -> str:
        dims = f"{self.width}×{self.height}" if self.width and self.height else "尺寸未知"
        return f"{self.path}（{self.mime}，{dims}，{self.size} 字节）"


@dataclass
class ImageInbox:
    """一次运行（一段）里攒下的图片，由 loop 在下一次模型请求前取走。"""

    max_images: int = MAX_IMAGES_PER_RUN
    max_bytes: int = MAX_IMAGE_BYTES
    _items: list[PendingImage] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self._items)

    def add(self, image: PendingImage) -> str | None:
        """收一张。返回 None = 收下了；返回字符串 = **拒绝原因**（给模型看）。"""
        if len(self._items) >= self.max_images:
            return (
                f"这一轮已经读入 {self.max_images} 张图片（上限），`{image.path}` 没有加进去。"
                "多图很吃上下文；请只挑关键的那一两张，或先用 bash 把图裁小/合并。"
            )
        if image.size > self.max_bytes:
            return (
                f"`{image.path}` 有 {image.size} 字节，超过单张上限 {self.max_bytes} 字节，"
                "没有读入。请先用 bash 压缩/裁剪它（或只读其中的关键区域）。"
            )
        self._items.append(image)
        return None

    def take(self) -> list[PendingImage]:
        """取走全部待注入图片（注入之后清空）。"""
        items, self._items = self._items, []
        return items


def data_url(mime: str, data: bytes) -> str:
    """OpenAI 兼容接口的图片形式：`data:<mime>;base64,<...>`。"""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def read_pending(path: Path, *, note: str = "") -> tuple[PendingImage | None, str | None]:
    """读一个文件并组装成 PendingImage。返回 (图片, 错误说明)。

    **先 stat 再读**：超过上限的文件直接拒绝、不进内存。先用 `read_bytes()` 再判大小
    的话，一张 500 MB 的图会先把整个进程的内存吃掉——而拒绝信息本来只需要知道大小。
    """
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError as exc:
        return None, f"读取失败：{exc}"
    if size > MAX_IMAGE_BYTES:
        return None, (
            f"`{target}` 有 {size} 字节，超过单张上限 {MAX_IMAGE_BYTES} 字节，**没有读它**"
            "（避免把大文件整个读进内存）。请先用 bash 压缩或裁剪。"
        )
    try:
        data = target.read_bytes()
    except OSError as exc:
        return None, f"读取失败：{exc}"
    mime = sniff_image(data)
    if mime is None:
        return None, (
            f"`{target}` 不是受支持的图片（按**文件签名**判断，不看扩展名）："
            "只支持 PNG / JPEG / WebP / GIF。"
        )
    dimensions = image_size(data, mime)
    return (
        PendingImage(
            path=str(target),
            mime=mime,
            width=dimensions[0] if dimensions else None,
            height=dimensions[1] if dimensions else None,
            size=len(data),
            note=note,
            data=data,
        ),
        None,
    )

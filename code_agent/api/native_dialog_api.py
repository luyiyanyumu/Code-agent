"""本机项目入口使用系统文件夹对话框，远程访问保留路径输入。"""
from __future__ import annotations

import ipaddress
import os

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.codeintel import native_dialog


def _loopback(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return address.is_loopback or bool(getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback)
    except ValueError:
        return False


def local_request(request: Request) -> bool:
    if not request.client or not _loopback(request.client.host):
        return False
    # 本机代理可转发请求，但远程代理来访不能在服务主机上弹窗。
    forwarded = request.headers.get("x-forwarded-for", "")
    return not forwarded or all(_loopback(host.strip()) for host in forwarded.split(","))


def picker_capabilities(request: Request) -> dict:
    info = native_dialog.capabilities()
    if os.environ.get("CODE_AGENT_CODE_BROWSE", "1") == "0":
        return {**info, "available": False, "reason": "目录选择已关闭，可手动输入工作区路径。"}
    if not local_request(request):
        return {**info, "available": False, "reason": "远程工作区请手动输入服务主机上的项目路径。"}
    return info


class FolderPick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str = Field(default="", max_length=4096)

    @field_validator("start")
    @classmethod
    def valid_path_text(cls, value):
        if "\x00" in value:
            raise ValueError("路径不能包含 NUL 字符")
        return value.strip()


def mount_native_dialog_api(app):
    @app.post("/api/v1/code/fs/pick-folder")
    async def pick_folder(request: Request, body: FolderPick, user: CurrentUser = Depends(get_current_user)):
        info = picker_capabilities(request)
        if not info["available"]:
            raise HTTPException(503 if local_request(request) else 403, info.get("reason") or "系统文件夹选择器不可用，请手动输入路径。")
        try:
            return await native_dialog.pick_folder(body.start)
        except native_dialog.NativeDialogBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except native_dialog.NativeDialogUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except native_dialog.NativeDialogError as exc:
            raise HTTPException(503, str(exc)) from exc


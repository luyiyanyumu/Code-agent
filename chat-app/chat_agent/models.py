"""Independent model profiles; private credentials never enter public views."""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from .providers import validate_base_url


class Models:
    def __init__(self, config, store=None):
        self.config = config
        self.path = Path(config.home) / "models.json"
        self._lock = threading.RLock()
        self._data = {"active": "offline", "profiles": []}
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError("模型配置文件无法读取，请修复或备份后重新配置") from exc
            if not isinstance(saved, dict) or not isinstance(saved.get("profiles"), list):
                raise ValueError("模型配置文件格式无效")
            self._data = saved
        self._environment = self._env_profile()

    def _env_profile(self):
        provider = str(getattr(self.config, "provider", "offline") or "offline")
        if provider == "offline":
            return None
        if provider not in {"openai-compatible", "deepseek"}:
            raise ValueError("CHAT_AGENT_PROVIDER 必须是 offline、openai-compatible 或 deepseek")
        base = getattr(self.config, "api_base", "") or ("https://api.deepseek.com/v1" if provider == "deepseek" else "https://api.openai.com/v1")
        model = getattr(self.config, "model", "") or ("deepseek-chat" if provider == "deepseek" else "")
        if not model:
            raise ValueError("使用环境变量配置在线模型时需要设置 CHAT_AGENT_MODEL")
        return {"id": "environment", "name": "环境变量配置", "provider": provider, "base_url": validate_base_url(base), "api_key": str(getattr(self.config, "api_key", "") or ""), "model": str(model), "supports_images": False, "readonly": True}

    def _all(self):
        profiles = [{"id": "offline", "name": "本地演示（无 AI）", "provider": "offline", "base_url": "", "api_key": "", "model": "offline-demo", "supports_images": False, "readonly": True}]
        if self._environment:
            profiles.append(self._environment)
        profiles.extend(self._data.get("profiles", []))
        return profiles

    def _active_id(self):
        # On first launch env config is the requested default. Persisted choice wins.
        active = self._data.get("active")
        if not self.path.exists() and self._environment:
            active = "environment"
        return active if any(p["id"] == active for p in self._all()) else "offline"

    def _public(self, profile):
        has_key = bool(profile.get("api_key"))
        provider = profile["provider"]
        model = profile.get("model", "")
        return {"id": profile["id"], "name": profile["name"], "provider": provider, "base_url": profile.get("base_url", ""), "model": model, "models": [{"id": model, "label": model}], "active": profile["id"] == self._active_id(), "has_key": has_key, "api_key_set": has_key, "model_default": model, "local": provider == "offline" or not has_key, "supports_images": bool(profile.get("supports_images")), "readonly": bool(profile.get("readonly"))}

    def profiles(self):
        with self._lock:
            return [self._public(profile) for profile in self._all()]

    def current(self):
        with self._lock:
            profile = next(p for p in self._all() if p["id"] == self._active_id())
            return {**profile, "request_timeout": float(getattr(self.config, "request_timeout", 120))}

    def public_settings(self):
        current = self.current()
        return {"active_profile_id": current["id"], "active_model": current["model"], "provider": current["provider"], "offline": current["provider"] == "offline", "supports_images": bool(current.get("supports_images"))}

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
                json.dump(self._data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("模型配置必须是对象")
        with self._lock:
            supplied_id = str(payload.get("id") or "")
            if supplied_id in {"offline", "environment"}:
                raise ValueError("内置或环境变量配置不能在界面中修改")
            existing = next((p for p in self._data["profiles"] if p["id"] == supplied_id), None)
            if supplied_id and existing is None:
                raise ValueError("模型配置不存在")
            provider = str(payload.get("provider") or (existing or {}).get("provider", "openai-compatible"))
            if provider not in {"openai-compatible", "deepseek"}:
                raise ValueError("请选择 OpenAI 兼容或 DeepSeek 服务")
            name = str(payload.get("name") or (existing or {}).get("name", "")).strip()
            model = str(payload.get("model") or (existing or {}).get("model", "")).strip()
            if not name or len(name) > 100 or not model or len(model) > 200:
                raise ValueError("请填写名称（最多 100 字符）和模型（最多 200 字符）")
            base = payload.get("base_url") or (existing or {}).get("base_url") or ("https://api.deepseek.com/v1" if provider == "deepseek" else "")
            api_key = payload.get("api_key")
            if api_key is not None and not isinstance(api_key, str):
                raise ValueError("API 密钥必须是字符串")
            key = str(api_key or (existing or {}).get("api_key", "")).strip()
            if payload.get("clear_key"):
                key = ""
            if len(key) > 8192 or "\n" in key or "\r" in key:
                raise ValueError("API 密钥格式无效")
            supports_images = payload.get("supports_images", (existing or {}).get("supports_images", False))
            if not isinstance(supports_images, bool):
                raise ValueError("图像支持选项必须是布尔值")
            profile = {"id": supplied_id or "model_" + uuid.uuid4().hex[:12], "name": name, "provider": provider, "base_url": validate_base_url(base), "model": model, "api_key": key, "supports_images": supports_images}
            original = self._data
            updated = [p for p in original["profiles"] if p["id"] != profile["id"]] + [profile]
            # Keep environment-selected default when first saving a non-active profile.
            self._data = {"active": self._active_id(), "profiles": updated}
            try:
                self._write()
            except OSError:
                self._data = original
                raise
            return self._public(profile)

    def activate(self, profile_id):
        with self._lock:
            if not any(p["id"] == profile_id for p in self._all()):
                raise ValueError("模型配置不存在")
            original = self._data
            self._data = {**original, "active": profile_id}
            try:
                self._write()
            except OSError:
                self._data = original
                raise
            return self.public_settings()

    def delete(self, profile_id):
        with self._lock:
            if profile_id in {"offline", "environment"}:
                raise ValueError("内置或环境变量配置不能删除")
            if not any(p["id"] == profile_id for p in self._data["profiles"]):
                raise ValueError("模型配置不存在")
            original = self._data
            self._data = {"active": "offline" if self._active_id() == profile_id else self._active_id(), "profiles": [p for p in original["profiles"] if p["id"] != profile_id]}
            try:
                self._write()
            except OSError:
                self._data = original
                raise

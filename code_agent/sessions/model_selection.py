"""Session-owned model selection, projected from saved connection catalogs.

Only provider/model/effort IDs are persisted on sessions. Credentials remain in the
existing settings store and never enter session metadata or browser catalogs.
"""
from __future__ import annotations

import json
from typing import Any

from code_agent import settings
from code_agent.providers.base import is_local_base_url
from code_agent.providers.capacity import effective_model_capacity
from code_agent.providers.reasoning import reasoning_metadata, resolve_reasoning_effort


def decode_selection(raw: Any) -> dict[str, str] | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if value.startswith("{"):
        try:
            data = json.loads(value)
            if isinstance(data, dict) and isinstance(data.get("provider"), str) and isinstance(data.get("model"), str):
                selection = {"provider": data["provider"], "model": data["model"]}
                if isinstance(data.get("reasoningEffort"), str):
                    selection["reasoningEffort"] = data["reasoningEffort"]
                return selection
        except ValueError:
            pass
    # Older sessions stored a plain concrete model, against the default route.
    return {"provider": "default", "model": value}


def configured(config: dict) -> bool:
    return config.get("provider") == "offline" or bool(config.get("api_key")) or is_local_base_url(str(config.get("base_url") or ""))


def catalog(config: dict, default: str) -> list[dict[str, Any]]:
    result = settings.sanitize_models(config.get("models"))
    if default and not any(row["id"] == default for row in result):
        result.insert(0, {"id": default, "name": default})
    for row in result:
        metadata = reasoning_metadata(str(config.get("provider") or ""), row["id"])
        if metadata is not None:
            row["reasoning"] = metadata
    return result


class SessionModelDirectory:
    def __init__(self, router, repository):
        self.router, self.repository = router, repository

    def _connections(self):
        current = settings.model_config(self.router.provider_name)
        current_model = self.router.model_name()
        current["models"] = settings.load().get("models", [])
        store = settings._read_profiles()
        active = settings._find_profile(store, store.get("active", ""))
        if active and all(str(active.get(field) or "") == str(current.get(field) or "") for field in ("provider", "base_url", "api_key")):
            current["models"] = active.get("models", current["models"])
        result = [("default", "全局默认连接", current, current_model)]
        for row in store["profiles"]:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            result.append((str(row["id"]), str(row.get("name") or row["id"]), row, str(row.get("model_default") or "")))
        return result

    def view(self, meta: dict) -> dict:
        connections = self._connections()
        groups = [{"id": pid, "name": name, "configured": configured(config),
                   "models": catalog(config, default)} for pid, name, config, default in connections]
        default = {"provider": "default", "model": connections[0][3]}
        default_effort = resolve_reasoning_effort(str(connections[0][2].get("provider") or ""), default["model"])
        if default_effort is not None:
            default["reasoningEffort"] = default_effort
        saved = decode_selection(meta.get("model_selection"))
        current = dict(saved or default)
        group = next((group for group in groups if group["id"] == current["provider"]), None)
        model = next((row for row in group["models"] if row["id"] == current["model"]), None) if group else None
        known = model is not None
        available = bool(known and group["configured"])
        reason = "" if available else (
            "所选提供方已移除，请重新选择模型" if group is None else
            "所选模型已不在提供方目录中，请重新选择" if not known else
            "所选提供方尚未配置密钥，请在模型设置中完成配置")
        if model is not None:
            reasoning = model.get("reasoning") or {}
            effort = current.get("reasoningEffort", reasoning.get("defaultEffort"))
            if effort is not None:
                current["reasoningEffort"] = effort
                if effort not in {entry["id"] for entry in reasoning.get("efforts", [])}:
                    available = False
                    reason = "所选推理强度已不可用，请重新选择"
        return {"groups": groups, "current": current, "default": default,
                "available": available, "reason": reason, "inherited": saved is None}

    def _selection_details(self, provider: str, model: str, reasoning_effort: str | None = None):
        for pid, _name, config, default in self._connections():
            if pid != provider:
                continue
            if not configured(config):
                raise ValueError("所选模型连接尚未配置，请在模型设置中完成配置")
            if not any(row["id"] == model for row in catalog(config, default)):
                raise ValueError("所选模型已不在提供方目录中，请重新选择")
            effort = resolve_reasoning_effort(str(config.get("provider") or ""), model, reasoning_effort)
            selection = {"provider": provider, "model": model}
            if effort is not None:
                selection["reasoningEffort"] = effort
            return selection, config
        raise ValueError("所选提供方已移除，请重新选择模型")

    def validate_selection(self, provider: str, model: str, reasoning_effort: str | None = None) -> dict[str, str]:
        return self._selection_details(provider, model, reasoning_effort)[0]

    def resolve(self, selection: dict):
        selected, config = self._selection_details(selection["provider"], selection["model"], selection.get("reasoningEffort"))
        return self.router.resolve_configured_provider(config, selected["model"], selected.get("reasoningEffort"))

    def router_for(self, session_id: str):
        return SessionModelRouter(self, session_id)


class SessionModelRouter:
    """An existing loop keeps its provider; future loop construction re-resolves."""
    def __init__(self, directory: SessionModelDirectory, session_id: str):
        self.directory, self.session_id = directory, session_id

    def _selection(self):
        row = self.directory.repository.get(self.session_id)
        return decode_selection((row or {}).get("model_selection"))

    def resolve_default(self):
        if self.directory.router.provider_override is not None:
            return self.directory.router.resolve_default()
        selection = self._selection()
        return self.directory.resolve(selection) if selection else self.directory.router.resolve_default()

    def model_name(self):
        selection = self._selection()
        return selection["model"] if selection else self.directory.router.model_name()

    def model_capacity(self, provider=None):
        # A loop may hold a profile provider after its UI selection was reset
        # or changed. Its already captured limits must not become global limits.
        if provider is not None and (getattr(provider, "model", "") or hasattr(provider, "context_window")):
            return effective_model_capacity(getattr(provider, "model", "") or self.model_name(),
                                            getattr(provider, "context_window", None),
                                            getattr(provider, "max_output_tokens", None))
        if self.directory.router.provider_override is not None or not self._selection():
            return self.directory.router.model_capacity(provider)
        resolved = provider if provider is not None else self.resolve_default()
        return effective_model_capacity(getattr(resolved, "model", "") or self.model_name(),
                                        getattr(resolved, "context_window", None),
                                        getattr(resolved, "max_output_tokens", None))

    @property
    def provider_override(self):
        return self.directory.router.provider_override

    @provider_override.setter
    def provider_override(self, provider):
        self.directory.router.provider_override = provider

    def __getattr__(self, name):
        return getattr(self.directory.router, name)

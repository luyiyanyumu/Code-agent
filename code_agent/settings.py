"""运行时配置（M13）：页面可改的大模型配置，持久化到 ~/.code-agent/config.json。

优先级：config.json（页面保存）> 环境变量（.env 已并入）> 代码默认值。
即用户在「设置」页保存的值代表最新意图、覆盖 .env；不写 config.json 时
行为与原来完全一致（吃环境变量 / .env / 默认）。

安全：api_key 明文落在 config.json（本地数据目录），读接口只回打码提示，
不回传完整 key；保存时 api_key 留空表示「不修改」，保留原值。
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from code_agent.config import agent_home
from code_agent.permissions.hooks import PERMISSION_MODES
from code_agent.providers.base import is_local_base_url
from code_agent.providers.capacity import MAX_MODEL_TOKENS, normalize_capacity
from code_agent.providers.network import normalize_proxy_mode


def settings_file() -> Any:
    """config.json 路径（动态求值：跟随 CODE_AGENT_HOME 变化，测试隔离用）。"""
    return agent_home() / "config.json"

#: 各 provider 可选的 api_key / base_url 环境变量名（config.json 未设时兜底）
_PROVIDER_ENV = {
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "openai_compat": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "offline": ("", ""),
}

#: 默认模型名的环境变量（M36：三档 lite/default/craft 已去掉，只留一个默认模型）
_MODEL_ENV = "CODE_AGENT_MODEL_DEFAULT"

# Limits are optional overrides. An empty field restores the model's automatic
# capacity instead of storing zero or an unusable request limit.
MODEL_TOKEN_FIELDS = ("context_window", "max_output_tokens")


def normalize_token_limit(value: Any, field: str) -> int | None:
    return normalize_capacity(value, field)


def _configured_token_limits(values: dict[str, Any]) -> dict[str, int | None]:
    """Read optional limits defensively from existing, manually editable files."""
    limits: dict[str, int | None] = {}
    for field in MODEL_TOKEN_FIELDS:
        try:
            limits[field] = normalize_token_limit(values.get(field), field)
        except ValueError:
            limits[field] = None
    return limits


def _capacity_model_name(values: dict[str, Any]) -> str:
    """Match the router/provider fallback without importing the router here."""
    import os

    provider = values.get("provider") or os.environ.get("CODE_AGENT_PROVIDER") or "deepseek"
    if provider == "offline":
        return "offline-mock"
    model = str(values.get("model_default") or os.environ.get(_MODEL_ENV, "")).strip()
    if model:
        return model
    if provider == "deepseek":
        return "deepseek-chat"
    return os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"


def _validate_model_token_limits(values: dict[str, Any]) -> None:
    from code_agent.providers.capacity import effective_model_capacity

    effective_model_capacity(_capacity_model_name(values), **_configured_token_limits(values))


def _effective_profile_token_limits(values: dict[str, Any]) -> dict[str, int | None]:
    from code_agent.providers.capacity import effective_model_capacity

    limits = effective_model_capacity(_capacity_model_name(values), **_configured_token_limits(values))
    return {f"effective_{field}": limits[field] for field in MODEL_TOKEN_FIELDS}


def load() -> dict[str, Any]:
    """读取整个 config.json（不存在或损坏返回空 dict）。

    用 utf-8-sig：Windows 编辑器常写入 BOM，带 BOM 时 json.loads 会失败，
    整份设置被当成"没配过"——用户会看到自己保存的配置莫名失效。
    """
    path = settings_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            # Removed execution-mode settings in old profiles are inert.
            data.pop("loop_mode", None)
            return data
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def permission_mode() -> str:
    """全局权限档位（config.json 的 permission_mode，作用于新建会话）。"""
    mode = load().get("permission_mode") or "workspace-write"
    return mode if mode in PERMISSION_MODES else "workspace-write"


def save_permission_mode(mode: str) -> str:
    if mode not in PERMISSION_MODES:
        raise ValueError(f"未知权限档位：{mode}（可选：{', '.join(PERMISSION_MODES)}）")
    save({"permission_mode": mode})
    return mode


#: 模型清单（M40）：挂在**每条配置档案**下面，不再有全局的"模型目录"。
#: 每项 {name, id}：name 是给人看的显示名，id 是发给服务端的模型名。
MAX_CATALOG = 200


def sanitize_models(raw: Any) -> list[dict[str, str]]:
    """清洗模型清单：去掉空项、按 id 去重、限制条数与长度（**不静默保留脏数据**）。"""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw[:MAX_CATALOG]:
        if isinstance(item, str):
            name, mid = item, item
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("label") or "").strip()
            mid = str(item.get("id") or item.get("model") or "").strip()
        else:
            continue
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append({"name": (name or mid)[:80], "id": mid[:120]})
    return out


def legacy_model_catalog_purge() -> bool:
    """清除旧版全局模型目录和已移除的执行模式设置。

    模型目录已搬到配置档案；旧执行模式不再影响新建或恢复会话。
    返回是否真的清掉了东西（供测试断言）。
    """
    path = settings_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict) or not {"model_catalog", "loop_mode"}.intersection(data):
        return False
    for key in ("model_catalog", "loop_mode"):
        data.pop(key, None)
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return False
    return True


#: 契约闸门（M32 · R7）：契约文件（`*.CONTRACT.md`）对 AI 只读。
#:
#: 默认**开**。理由：R7 是契约骨架编程的命门——只要 AI 能动契约，它就会用"改契约"
#: 让自己通过检查（和"改测试凑通过"同一个病）。默认关等于"靠 prompt 自觉"，
#: 而阶段 0 实验里 4 次实现零违例恰恰是因为 prompt 里明确禁止了——**prompt 不是闸门**。
#: 真需要让 AI 改契约时，把这一项关掉（界面设置里），或是人自己改。
DEFAULT_CONTRACT_GUARD = True


def contract_guard() -> bool:
    """契约闸门是否开启（config.json 的 contract_guard，缺省为开）。"""
    raw = load().get("contract_guard")
    if isinstance(raw, bool):
        return raw
    return DEFAULT_CONTRACT_GUARD


def set_contract_guard(enabled: bool) -> bool:
    save({"contract_guard": bool(enabled)})
    return bool(enabled)


def task_permission_mode() -> str:
    """任务 / 车道会话用的权限档位：**跟随全局默认，但下限是 `workspace-write`**。

    这里原来在两处**硬编码** `workspace-write`（`codeintel/tasks.py` 的任务执行、
    `codeintel/parallel.py` 的车道执行），真机踩到：用户把默认档位调成「完全权限」之后，
    从代码页跑的任务**仍然每条命令都要人工审批**——因为任务会话被写死成 workspace-write，
    用户选什么都不影响它。表现就是"我明明选了免审批，还是一直弹审批"。

    为什么下限是 workspace-write（不跟随 read-only）：任务要在隔离工作树里改文件，
    只读档会让任务在写第一步就卡死。`read-only` 是"给主会话看地图"的档位，不是给执行的。

    全局默认可在「设置 → 权限档位」或 `settings.save_permission_mode()` 改。
    """
    mode = permission_mode()
    return "workspace-write" if mode == "read-only" else mode


#: 工具级审批策略（页面可改，覆盖内置策略）
#: - allow    免审批：该工具直接放行
#: - approval 需审批：每次调用都人工确认
#: - policy   按参数判定：用内置策略（如 bash 白名单/黑名单）；无覆盖时即此档
TOOL_POLICY_MODES = ("allow", "approval", "policy")


def tool_policies() -> dict[str, str]:
    """读工具级策略覆盖：{tool_name: "allow"|"approval"|"policy"}。非法值忽略。"""
    tp = load().get("tool_policies")
    if not isinstance(tp, dict):
        return {}
    return {k: v for k, v in tp.items() if v in TOOL_POLICY_MODES}


def set_tool_policy(tool: str, mode: str | None) -> dict[str, str]:
    """设置/清除某工具的审批策略。mode 为空或 "policy" = 清除覆盖（回退内置策略）。"""
    tp = tool_policies()
    if not mode or mode == "policy":
        tp.pop(tool, None)
    else:
        if mode not in TOOL_POLICY_MODES:
            raise ValueError(f"未知策略模式：{mode}（可选：{', '.join(TOOL_POLICY_MODES)}）")
        tp[tool] = mode
    save({"tool_policies": tp})
    return tp


#: 被用户**禁用**的工具名（config.json 的 disabled_tools）。
#:
#: 与"审批策略"是两件事：策略管"要不要人工确认"，禁用管"这个工具存不存在"。
#: 禁用后模型**看不到它**（不进 schema），也调不动（注册表层直接拒）——
#: 这与 M19 联网开关同一套做法：看得见的能力 = 调得动，看得见就少一轮试错。
def disabled_tools() -> list[str]:
    names = load().get("disabled_tools")
    if not isinstance(names, list):
        return []
    return sorted({str(n).strip() for n in names if str(n).strip()})


def set_tool_enabled(tool: str, enabled: bool) -> list[str]:
    """启用/禁用单个工具，返回禁用清单。"""
    names = set(disabled_tools())
    name = (tool or "").strip()
    if not name:
        raise ValueError("工具名不能为空")
    if enabled:
        names.discard(name)
    else:
        names.add(name)
    save({"disabled_tools": sorted(names)})
    return sorted(names)


def set_tools_enabled(tools: list[str], enabled: bool) -> list[str]:
    """批量启用/禁用（「一键禁用全部」走这里），返回禁用清单。

    传空列表 + enabled=False = 清空禁用清单（等价于全部启用）。
    """
    names = set(disabled_tools())
    targets = {str(t).strip() for t in (tools or []) if str(t).strip()}
    if enabled:
        names -= targets
        if not targets:
            names.clear()
    else:
        names |= targets
    save({"disabled_tools": sorted(names)})
    return sorted(names)


#: 被用户**停用**的技能/专家（config.json 的 disabled_packs），键是 `kind:name`
#: （kind = skill / expert）。停用后不进 system prompt 的技能目录、`load_skill` 也读不到——
#: 与工具一键禁用同一语义：看得见 = 用得上。
def disabled_packs() -> list[str]:
    names = load().get("disabled_packs")
    if not isinstance(names, list):
        return []
    return sorted({str(n).strip() for n in names if str(n).strip()})


def set_pack_enabled(kind: str, name: str, enabled: bool) -> list[str]:
    """启用/停用一个技能或专家，返回停用清单。"""
    kind = (kind or "").strip()
    name = (name or "").strip()
    if kind not in ("skill", "expert") or not name:
        raise ValueError(f"未知的技能类型或名字：{kind}:{name}")
    keys = set(disabled_packs())
    key = f"{kind}:{name}"
    if enabled:
        keys.discard(key)
    else:
        keys.add(key)
    save({"disabled_packs": sorted(keys)})
    return sorted(keys)


def save(data: dict[str, Any]) -> dict[str, Any]:
    """合并写入 config.json（只更新传入的 key，不丢其他字段）。

    值为 None 或空字符串的 key 会被**清除**——回退到环境变量 / 代码默认值，
    而不是把空字符串留成脏数据。
    """
    if "loop_mode" in data:
        raise ValueError("循环模式设置已移除")
    if "proxy_mode" in data and data["proxy_mode"] not in (None, ""):
        data = {**data, "proxy_mode": normalize_proxy_mode(data["proxy_mode"])}
    data = dict(data)
    for field in MODEL_TOKEN_FIELDS:
        if field in data:
            data[field] = normalize_token_limit(data[field], field)
    path = settings_file()
    merged = load()
    for k, v in data.items():
        if v in (None, ""):
            merged.pop(k, None)
        else:
            merged[k] = v
    _validate_model_token_limits(merged)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return merged


def model_config(provider: str | None = None) -> dict[str, Any]:
    """读大模型配置（config.json 优先，环境变量兜底）。返回语义化字段。

    provider 参数用于让 api_key / base_url 的环境变量兜底跟随**实际生效**的
    provider（例如 create_app 显式指定的 offline），而非配置层默认值。
    """
    import os

    cfg = load()
    provider = provider or cfg.get("provider") or os.environ.get("CODE_AGENT_PROVIDER") or "deepseek"
    api_key_env, base_url_env = _PROVIDER_ENV.get(provider, ("", ""))

    api_key = cfg.get("api_key") or ""
    if not api_key and api_key_env:
        api_key = os.environ.get(api_key_env, "")
    base_url = cfg.get("base_url") or ""
    if not base_url and base_url_env:
        base_url = os.environ.get(base_url_env, "")

    return {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
        "proxy_mode": normalize_proxy_mode(cfg.get("proxy_mode")),
        # M36：只有一个默认模型（三档已去掉）
        "model_default": cfg.get("model_default") or os.environ.get(_MODEL_ENV, ""),
        **_configured_token_limits(cfg),
    }


def mask_key(secret: str) -> str:
    """api_key 打码：保留前 3 后 4，中间打星号。"""
    if not secret:
        return ""
    if len(secret) <= 8:
        return secret[:2] + "*" * (len(secret) - 2) if len(secret) > 2 else "***"
    return f"{secret[:3]}{'*' * 6}{secret[-4:]}"


# ---------------------------------------------------------------------------
# 模型配置档案（M14）：多 provider 预设，一键切换当前生效配置。
# 档案存 providers.json（明文 key，与 config.json 同级安全域）；
# 「激活」= 把档案写入 config.json（复用 settings.save + router.reload 热生效）。
# ---------------------------------------------------------------------------

def _profiles_file() -> Any:
    return agent_home() / "providers.json"


#: 内置提供方预设：设置页「+ DeepSeek / + 本地 Ollama …」按钮用的**模板**——
#: 点一下按它新建一条**普通档案**（可编辑、可删除），模板本身不在档案列表里。
#: M36：每个预设只有一个默认模型（原来 lite/default/craft 三个字段已去掉——
#: "这次用便宜模型"由会话级选具体模型表达，不再靠档位猜任务难度）。
BUILTIN_PRESETS: list[dict[str, Any]] = [
    {"name": "DeepSeek", "provider": "deepseek", "base_url": "",
     "model_default": "deepseek-chat"},
    {"name": "Kimi", "provider": "openai_compat", "base_url": "https://api.moonshot.cn/v1",
     "model_default": "kimi-k2-turbo-preview"},
    {"name": "火山方舟", "provider": "openai_compat", "base_url": "https://ark.cn-beijing.volces.com/api/v3",
     "model_default": "deepseek-v4-pro"},
    {"name": "OpenAI", "provider": "openai", "base_url": "https://api.openai.com/v1",
     "model_default": ""},
    # 本地 / 内网后端：走 openai_compat 协议，通常无需 API Key（见 is_local_base_url）。
    # 模型名一律留空——各人本机拉取的模型不同，交给「获取可用模型」从 /v1/models
    # 拉取后填入，写死一个名字只会换来 404 model not found。
    {"name": "本地 Ollama", "provider": "openai_compat", "base_url": "http://127.0.0.1:11434/v1",
     "model_default": ""},
    {"name": "本地 LM Studio", "provider": "openai_compat", "base_url": "http://127.0.0.1:1234/v1",
     "model_default": ""},
    {"name": "本地 vLLM", "provider": "openai_compat", "base_url": "http://127.0.0.1:8000/v1",
     "model_default": ""},
]


def _profile_view(p: dict[str, Any], active_id: str) -> dict[str, Any]:
    key = p.get("api_key") or ""
    base_url = p.get("base_url") or ""
    return {
        "id": p["id"],
        "name": p["name"],
        "provider": p["provider"],
        "base_url": base_url,
        "proxy_mode": normalize_proxy_mode(p.get("proxy_mode")),
        "model_default": p.get("model_default") or "",
        **_configured_token_limits(p),
        **_effective_profile_token_limits(p),
        "api_key_set": bool(key),
        "api_key_hint": mask_key(key),
        "local": is_local_base_url(base_url),
        "active": p["id"] == active_id,
        # M40：这条档案自己的模型清单（模型目录搬到了档案下面）
        "models": sanitize_models(p.get("models")),
    }


def _read_profiles() -> dict[str, Any]:
    """读 providers.json（只含用户自己建/加的档案）。

    M36 起**不再播种**"当前配置"档案：那个假档案代表的是"此刻生效的配置"，
    而它本身就在设置页上方单独编辑（"编辑当前生效配置"），列在档案列表里只会让人
    分不清"编辑这条"和"编辑当前配置"有什么区别；它又是预设不可删，用户想去掉也去不掉。
    所以这里顺手把老数据里的它**清掉**（不是藏起来），并把 active 修正为"没有生效档案"。
    """
    path = _profiles_file()
    data: dict[str, Any] | None = None
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("profiles"), list):
                data = loaded
        except (json.JSONDecodeError, OSError):
            data = None

    if data is None:
        return {"active": "", "profiles": []}

    dropped = 0
    cleaned = 0
    profiles: list[dict[str, Any]] = []
    for raw in data["profiles"]:
        if _is_legacy_seed(raw):
            dropped += 1
            continue
        # 顺手清掉去档位后遗留的字段（model_lite / model_craft）——留着会让人以为还有三档
        for dead in ("model_lite", "model_craft"):
            if dead in raw:
                raw.pop(dead)
                cleaned += 1
        profiles.append(raw)

    active = data.get("active") or ""
    repaired = bool(active) and not any(p.get("id") == active for p in profiles)
    if dropped or cleaned or repaired:
        # 清理落盘：下次读取不必重复判断（也让用户直接看到文件里也干净了）
        _write_profiles({"active": "" if repaired else active, "profiles": profiles})
    return {"active": "" if repaired else active, "profiles": profiles}


def _is_legacy_seed(profile: dict[str, Any]) -> bool:
    """老版本自动播种的"当前配置"档案（id=mp_seed 或 preset 标记的那个）。"""
    if not isinstance(profile, dict):
        return True
    return profile.get("id") == "mp_seed" or (
        bool(profile.get("preset")) and str(profile.get("name") or "") == "当前配置"
    )


def _write_profiles(data: dict[str, Any]) -> None:
    path = _profiles_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_profiles() -> list[dict[str, Any]]:
    data = _read_profiles()
    return [_profile_view(p, data.get("active", "")) for p in data["profiles"]]


def _find_profile(data: dict[str, Any], pid: str) -> dict[str, Any] | None:
    return next((p for p in data["profiles"] if p.get("id") == pid), None)


def profile_models(pid: str) -> dict[str, Any]:
    """某条档案的模型清单（M40：模型目录挂在档案下面，不再有全局目录）。"""
    data = _read_profiles()
    p = _find_profile(data, pid)
    if p is None:
        raise KeyError("档案不存在")
    return {"id": pid, "items": sanitize_models(p.get("models"))}


def save_profile_models(pid: str, raw: Any) -> dict[str, Any]:
    """整份替换某条档案的模型清单。清洗规则与别处一致（去空、按 id 去重、限长）。"""
    data = _read_profiles()
    p = _find_profile(data, pid)
    if p is None:
        raise KeyError("档案不存在")
    p["models"] = sanitize_models(raw)
    _write_profiles(data)
    return profile_models(pid)


def reset_profile_models(pid: str) -> dict[str, Any]:
    """恢复默认模型：清掉这条档案自定义的清单（回到"只有它自己的默认模型"）。

    与 DSH 的「恢复默认模型」同义：清掉覆盖，而不是去猜一份新的。
    """
    data = _read_profiles()
    p = _find_profile(data, pid)
    if p is None:
        raise KeyError("档案不存在")
    p.pop("models", None)
    _write_profiles(data)
    return profile_models(pid)


def save_profile(data_in: dict[str, Any]) -> dict[str, Any]:
    """新建或更新档案。api_key 留空 = 不修改（新建留空则无 key）。返回档案视图。"""
    from code_agent.agent.router import KNOWN_PROVIDERS

    store = _read_profiles()
    pid = (data_in.get("id") or "").strip()
    previous = _find_profile(store, pid) if pid else None
    if pid and previous is None:
        raise KeyError("档案不存在")
    # 修改网络模式、名称等局部字段时，保留未提交的连接配置。
    # 明确传入空字符串仍可清空 Base URL / 模型；空 key 继续表示不修改。
    values = {**(previous or {}), **data_in}
    provider = (values.get("provider") or "").strip() or "deepseek"
    if provider not in KNOWN_PROVIDERS:
        raise ValueError(f"未知 provider：{provider}（可选：{', '.join(KNOWN_PROVIDERS)}）")
    base_url = (values.get("base_url") or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头")

    fields = {
        "provider": provider,
        "base_url": base_url,
        "model_default": (values.get("model_default") or "").strip(),
    }
    limits = _configured_token_limits(values)
    for field in MODEL_TOKEN_FIELDS:
        if field in data_in:
            limits[field] = normalize_token_limit(data_in[field], field)
        if (field in data_in or not pid) and limits[field] is not None:
            fields[field] = limits[field]
    _validate_model_token_limits({**values, **fields, **limits})
    if "proxy_mode" in data_in or not pid:
        fields["proxy_mode"] = normalize_proxy_mode(data_in.get("proxy_mode"))
    # 本地 / 内网后端没有"厂商默认模型"可兜底（openai_compat 的 default_model 是
    # gpt-4o-mini，打到 Ollama 上是必然的 404）——模型名必须显式给出。
    if is_local_base_url(base_url) and not fields["model_default"]:
        raise ValueError(
            "本地 / 内网后端必须填写模型名：点上面的「获取可用模型」拉取本机已加载的模型，"
            "选一个填进「默认模型」即可"
        )
    if pid:
        p = _find_profile(store, pid)
        if p is None:
            raise KeyError("档案不存在")
        name = (data_in.get("name") or "").strip()
        if name:
            p["name"] = name[:40]
        p.update(fields)
        for field in MODEL_TOKEN_FIELDS:
            if field in data_in and limits[field] is None:
                p.pop(field, None)
        new_key = (data_in.get("api_key") or "").strip()
        if new_key:
            p["api_key"] = new_key
        # 模型清单可以随档案一起存（不传就保持原样，避免"编辑档案"顺手清空清单）
        if data_in.get("models") is not None:
            p["models"] = sanitize_models(data_in.get("models"))
    else:
        name = (data_in.get("name") or "").strip()
        if not name:
            raise ValueError("档案名称不能为空")
        if any(p["name"] == name for p in store["profiles"]):
            raise ValueError(f"档案名称已存在：{name}")
        pid = f"mp_{secrets.token_hex(6)}"
        store["profiles"].append({
            "id": pid, "name": name[:40], "models": sanitize_models(data_in.get("models")),
            "api_key": (data_in.get("api_key") or "").strip(), **fields,
        })
    _write_profiles(store)
    return _profile_view(_find_profile(store, pid), store.get("active", ""))


def delete_profile(pid: str) -> None:
    """删掉一条档案。删的若是"生效中"那条，只把生效标记清掉。

    生效标记只是"这条被启用过"的记号，真正的生效配置写在 config.json 里——
    所以删掉档案不该顺手改用户当前的配置，也不该因为"它就是当前配置"而拒绝删除。
    """
    store = _read_profiles()
    p = _find_profile(store, pid)
    if p is None:
        raise KeyError("档案不存在")
    store["profiles"] = [x for x in store["profiles"] if x.get("id") != pid]
    if store.get("active") == pid:
        store["active"] = ""
    _write_profiles(store)


def activate_profile(pid: str) -> dict[str, Any]:
    """激活档案：写入 config.json 并返回热生效所需的更新字典（调用方再 router.reload）。"""
    store = _read_profiles()
    p = _find_profile(store, pid)
    if p is None:
        raise KeyError("档案不存在")
    updates = {
        "provider": p["provider"],
        "base_url": p.get("base_url") or "",
        "model_default": p.get("model_default") or "",
        "proxy_mode": normalize_proxy_mode(p.get("proxy_mode")),
        # Older profiles have no overrides. Switching to them must remove the
        # previous profile's budgets rather than silently keep using them.
        **_configured_token_limits(p),
    }
    if p.get("api_key"):
        updates["api_key"] = p["api_key"]
    save(updates)  # 值为空的 key 会被清除 → 回退环境变量/默认值，语义正确
    store["active"] = pid
    _write_profiles(store)
    return updates


# Agent hierarchy limits are enforced by the runtime, independently of prompts.
# Lead occupies depth 0; background child tasks begin at depth 1.
DEFAULT_AGENT_TEAM_CONFIG = {"max_depth": 3, "max_parallel": 4}
AGENT_TEAM_LIMITS = {"max_depth": (1, 8), "max_parallel": (1, 16)}


def agent_team_config() -> dict[str, int]:
    """Read bounded collaboration limits, tolerating manually damaged config."""
    raw = load().get("agent_team")
    raw = raw if isinstance(raw, dict) else {}
    config = dict(DEFAULT_AGENT_TEAM_CONFIG)
    for field, (lower, upper) in AGENT_TEAM_LIMITS.items():
        value = raw.get(field)
        if type(value) is int and lower <= value <= upper:
            config[field] = value
    return config


def save_agent_team_config(body: dict[str, Any]) -> dict[str, int]:
    """Validate before persistence and preserve all unrelated settings."""
    if not isinstance(body, dict):
        raise ValueError("智能体协作设置必须是对象")
    unknown = set(body) - set(AGENT_TEAM_LIMITS)
    if unknown:
        raise ValueError("未知智能体协作设置：" + ", ".join(sorted(map(str, unknown))))
    config = agent_team_config()
    labels = {"max_depth": "最大递归深度", "max_parallel": "并行子智能体上限"}
    for field, value in body.items():
        lower, upper = AGENT_TEAM_LIMITS[field]
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{labels[field]}必须是 {lower} 到 {upper} 的整数")
        config[field] = value
    save({"agent_team": config})
    return config


DEFAULT_AGENT_TEAM_BUDGETS = {"task_tokens": 0, "task_seconds": 0, "team_tokens": 0, "team_seconds": 0}
AGENT_TEAM_BUDGET_LIMITS = {"task_tokens": 10_000_000, "task_seconds": 86_400,
                          "team_tokens": 100_000_000, "team_seconds": 604_800}


def agent_team_budget_config() -> dict[str, int]:
    raw = load().get("agent_team_budgets")
    raw = raw if isinstance(raw, dict) else {}
    return {key: raw[key] if type(raw.get(key)) is int and 0 <= raw[key] <= upper else 0
            for key, upper in AGENT_TEAM_BUDGET_LIMITS.items()}


def save_agent_team_budgets(body: dict[str, Any]) -> dict[str, int]:
    if not isinstance(body, dict) or set(body) - set(AGENT_TEAM_BUDGET_LIMITS):
        raise ValueError("智能体预算必须是对象且不能包含未知字段")
    config = agent_team_budget_config()
    for key, value in body.items():
        if type(value) is not int or not 0 <= value <= AGENT_TEAM_BUDGET_LIMITS[key]:
            raise ValueError(f"{key} 必须是 0 到 {AGENT_TEAM_BUDGET_LIMITS[key]} 的整数；0 表示关闭")
        config[key] = value
    save({"agent_team_budgets": config})
    return config

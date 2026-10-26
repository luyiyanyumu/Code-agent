"""运行时配置：目录布局与环境变量。

约定（对应设计文档 §3）：
    ~/.code-agent/
    ├── transcripts/<session_id>.jsonl   # 事件轨迹（追加写）
    ├── tool_results/<session_id>/...    # 大输出外部化（M3）
    ├── artifacts/<session_id>/...       # 交付物（M5+）
    └── workspace/                       # 工具默认工作区（shell/文件读写根目录）

.env 加载（M9）：`.env.example` 一直让用户「复制为 .env 后填写」，但进程从未
读过它——配置只能靠手工 set 环境变量。下面的 load_env_file() 补上这一环，
且**零依赖**（不引入 python-dotenv，避免为一个十几行的解析器加依赖）。

优先级：真实环境变量 > .env > 代码内默认值。
即 .env 只填空位，绝不覆盖已存在的环境变量（CI / 容器注入的值始终优先）。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 已加载过的 .env 路径，避免重复解析
_LOADED_ENV_FILES: set[Path] = set()


def _parse_env_line(line: str) -> tuple[str, str] | None:
    """解析一行 KEY=VALUE。支持 # 注释、空行、export 前缀、单双引号。"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].lstrip()
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value


def load_env_file(path: Path | None = None, *, override: bool = False) -> Path | None:
    """把 .env 读进 os.environ。返回实际加载的文件路径（没找到则 None）。

    path 为 None 时按顺序找：$CODE_AGENT_ENV_FILE → cwd/.env → 包上一级/.env。
    override=False（默认）时，已存在的环境变量不会被覆盖——这样 .env 只做
    本地兜底，部署时由容器/CI 注入的值永远优先。
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    else:
        explicit = os.environ.get("CODE_AGENT_ENV_FILE")
        if explicit:
            candidates.append(Path(explicit))
        candidates.append(Path.cwd() / ".env")
        candidates.append(Path(__file__).resolve().parents[1] / ".env")

    for candidate in candidates:
        if not candidate.is_file():
            continue
        if candidate in _LOADED_ENV_FILES:
            return candidate
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            parsed = _parse_env_line(raw)
            if parsed is None:
                continue
            key, value = parsed
            if override or key not in os.environ:
                os.environ[key] = value
        _LOADED_ENV_FILES.add(candidate)
        return candidate
    return None


# ---------------------------------------------------------------------------
# M25 改名兼容层：CH-agent → Code-agent
#
# 改名涉及包名 / 环境变量 / 数据目录 / 启动脚本四层。已经装过的人不该因为改名而
# "打不开、数据没了"，所以这里做三件兼容（都是幂等、只做一次）：
#   1) 环境变量：旧前缀 `CH_AGENT_*` 自动搬到新前缀 `CODE_AGENT_*`（新名优先，
#      旧名只在没有新名时生效）。用户 .env / 计划任务 / 快捷方式里写的旧变量继续管用。
#   2) 数据目录：`~/.ch-agent` 若存在而 `~/.code-agent` 不存在 → **整目录拷贝**过来，
#      并把 `ch_agent.db*` 改名为 `code_agent.db*`。旧目录**保留**（当备份，不删）。
#   3) 只认一次：迁移完成后写一个 `.migrated-from-ch-agent` 标记，避免重复拷贝。
# ---------------------------------------------------------------------------

#: 旧 → 新 环境变量前缀（只搬没有新名的那些）
_LEGACY_ENV_PREFIX = "CH_AGENT_"
_ENV_PREFIX = "CODE_AGENT_"


def migrate_legacy_env() -> list[str]:
    """把旧的 `CH_AGENT_*` 环境变量搬到 `CODE_AGENT_*`（新名优先）。返回搬过的键。"""
    moved: list[str] = []
    for key, value in list(os.environ.items()):
        if not key.startswith(_LEGACY_ENV_PREFIX):
            continue
        new_key = _ENV_PREFIX + key[len(_LEGACY_ENV_PREFIX) :]
        if new_key in os.environ:
            continue          # 新名已显式设置 → 以新名为准
        os.environ[new_key] = value
        moved.append(f"{key} → {new_key}")
    return moved


def _legacy_home() -> Path:
    return Path.home() / ".ch-agent"


def migrate_legacy_home() -> Path | None:
    """`~/.ch-agent` → `~/.code-agent`（整目录拷贝 + DB 改名）。返回迁移到的目录。

    安全约束（都很重要）：
    - 只在目标目录**不存在或为空**时迁移，绝不覆盖已有数据；
    - 旧目录**复制不删除**（用户可确认后自行清理）；
    - 数据库主文件与其 `-wal`/`-shm` 一起改名，否则 SQLite 会看到一个"半套"库；
    - **只对默认数据目录迁移**：显式设了 `CODE_AGENT_HOME`（测试 / 临时实例）时一律不动 ——
      真机踩到：临时实例启动时把用户真实数据整份拷进了自己的 home，
      于是"临时环境"里冒出用户的历史会话，既浪费又让人看不懂。
    """
    import shutil  # 局部导入：不迁移时不引入

    if os.environ.get(_ENV_PREFIX + "HOME"):
        return None
    legacy = _legacy_home()
    target = Path(os.environ.get(_ENV_PREFIX + "HOME", str(Path.home() / ".code-agent")))
    marker = target / ".migrated-from-ch-agent"
    if marker.exists() or not legacy.is_dir() or not any(legacy.iterdir()):
        return None
    if target.exists() and any(target.iterdir()):
        return None           # 已有数据：不碰，交给用户决定
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(legacy, target, dirs_exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        old_db = target / f"ch_agent.db{suffix}"
        if old_db.exists():
            old_db.replace(target / f"code_agent.db{suffix}")
    marker.write_text(
        f"migrated from {legacy}\n原始目录保留未删，确认无误后可自行删除。\n",
        encoding="utf-8",
    )
    return target


def _boot_migration() -> None:
    """进程启动时跑一次（幂等）。放在 import 期，保证任何入口都生效。"""
    if os.environ.get(_ENV_PREFIX + "SKIP_MIGRATION"):
        return
    try:
        migrate_legacy_env()
        migrate_legacy_home()
    except OSError:
        pass      # 迁移失败不该拦启动：用户仍可用新目录从零开始


_boot_migration()


def agent_home() -> Path:
    """Code-agent 数据根目录，可用 CODE_AGENT_HOME 覆盖（测试隔离用）。"""
    return Path(os.environ.get("CODE_AGENT_HOME", str(Path.home() / ".code-agent")))


def auth_enabled() -> bool:
    """默认本地单用户免登录；显式开启后保留原来的 JWT 认证。"""
    value = os.environ.get("CODE_AGENT_AUTH_ENABLED", "false").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError("CODE_AGENT_AUTH_ENABLED 必须为 true 或 false")


def transcripts_dir() -> Path:
    p = agent_home() / "transcripts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def artifacts_dir() -> Path:
    p = agent_home() / "artifacts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tool_results_dir() -> Path:
    """大输出外部化的落盘根目录（M3，s13）。"""
    p = agent_home() / "tool_results"
    p.mkdir(parents=True, exist_ok=True)
    return p


def users_dir() -> Path:
    """用户级记忆根目录：users/<user_id>/MEMORY.md（M3，s11）。"""
    p = agent_home() / "users"
    p.mkdir(parents=True, exist_ok=True)
    return p


def skills_dir() -> Path:
    """Skills 根目录：skills/<name>/SKILL.md（M4，s16）。"""
    p = agent_home() / "skills"
    p.mkdir(parents=True, exist_ok=True)
    return p


def experts_dir() -> Path:
    """Experts 根目录：experts/<name>/EXPERT.md（M4，s18）。"""
    p = agent_home() / "experts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def builtin_skills_dir() -> Path:
    """**随包发布**的技能目录（只读）：`code_agent/skills/<name>/SKILL.md`（M49）。

    与 `skills_dir()`（用户目录，可写）分成两个来源，理由有两条：

    1. **优先级**：用户目录排在第一，插件次之，自带技能最后——用户可以覆盖同名的
       自带技能，而插件覆盖不了平台的（`packs.py` 的既定口径）。所以自带来源是
       "兜底"，不是"最高权"。
    2. **可删性**：自带技能躺在安装目录里。用户删不掉它（也不该删——下次升级又回来，
       而"我明明删过"是最容易让人不信任平台的那种反馈）；要停用它走 `disabled_packs`
       （停用清单），和停用别的技能同一条路。

    这里**不 mkdir**：只读来源不该因为被读一下就产生目录。
    """
    return Path(__file__).resolve().parent / "skills"


def mcp_config_path() -> Path:
    """MCP 连接器配置：~/.code-agent/mcp.json（M4，s17）。"""
    return agent_home() / "mcp.json"


def plugins_dir() -> Path:
    """插件包根目录：plugins/<name>/plugin.json（M10）。

    一个插件包可以同时声明内嵌技能、MCP 服务器、权限放行名单与 prompt 片段，
    是 Skills / Experts / MCP 之上的一层「打包分发」封装。
    """
    p = agent_home() / "plugins"
    p.mkdir(parents=True, exist_ok=True)
    return p


def workspace_root() -> Path:
    """工具执行的默认工作区根目录。"""
    p = agent_home() / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


def code_dir() -> Path:
    """代码模式的私有数据根目录：~/.code-agent/code/<project_id>/。

    索引、patch 台账、隔离工作树都在这里——**绝不写进用户自己的仓库**，
    这样"代码模式"出问题时用户的工程目录依然干净。
    """
    p = agent_home() / "code"
    p.mkdir(parents=True, exist_ok=True)
    return p


def projects_root() -> Path:
    """「新建项目」的默认落点：~/.code-agent/projects/<name>。

    用户「打开」的既有仓库不受影响；只有平台自己创建的工程才允许连目录一起删。
    """
    p = agent_home() / "projects"
    p.mkdir(parents=True, exist_ok=True)
    return p


def knowledge_dir() -> Path:
    """知识库根目录：knowledge/<doc_id>.md（M12）。

    每篇文档一个 markdown 文件，frontmatter 存 title/tags，正文是知识内容。
    支持页面编辑 / 文件上传 / 本地目录扫描三种来源，检索走向量（可选）+ BM25 降级。
    """
    p = agent_home() / "knowledge"
    p.mkdir(parents=True, exist_ok=True)
    return p

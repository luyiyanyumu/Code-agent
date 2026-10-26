"""代码情报（M18 代码模式）：把仓库解析成"项目地图"，供前端与模型共用。

分层（P0→P7 逐步长出来的，这里是**当前**的样子）：
- paths        路径安全边界（项目根校验 / 相对路径解析 / 忽略规则）
- projects     项目服务（打开、新建、树、文件读取、目录选择器）
- scanner      目录扫描（忽略规则 / 密钥与二进制探测 / 文件数上限）
- graph/store  符号索引与跨文件边（SQLite，每棵工作树一份）
- service      索引服务门面：地图 / 增量 / 诊断 / 影响面 / 契约角标 / 改动台账
- vcs          只读仓库探测（分支/远端/脏）；写操作仍受 `check_command` 黑名单约束
- tasks        任务隔离（git worktree + Plan 审批 + 符号级审查 + 选择性合并）
- parallel     并行车道（写租约 / 波次 / 串行合并队列）
- lsp*         语言服务器代理（28 个预设 + 运行时探测，不捆绑二进制）
- spec / purpose / logic / logic_brace  行为卡片、作用说明、实现步骤（“测试即规格”那一档）

导出只放"别处真的要用到的边界"；内部实现留在各自模块里。
"""

from code_agent.codeintel.paths import (
    ProjectPathError,
    is_ignored_dir,
    normalize_rel,
    resolve_project_root,
    root_key,
    safe_child,
)
from code_agent.codeintel.projects import ProjectService, detect_language, is_secret_file
from code_agent.codeintel.service import CodeIntelService
from code_agent.codeintel.store import SymbolIndex

__all__ = [
    "CodeIntelService",
    "ProjectPathError",
    "ProjectService",
    "SymbolIndex",
    "detect_language",
    "is_ignored_dir",
    "is_secret_file",
    "normalize_rel",
    "resolve_project_root",
    "root_key",
    "safe_child",
]

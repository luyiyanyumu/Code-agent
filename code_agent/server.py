"""服务入口：python -m code_agent.server --port 8765

启动时自动加载项目根目录的 .env（真实环境变量优先，.env 只填空位），
所以「复制 .env.example 为 .env 并填好」即可跑起来，无需手工 set。

环境变量：
    CODE_AGENT_PROVIDER        deepseek | openai | openai_compat | offline（默认 deepseek）
    CODE_AGENT_AUTH_ENABLED    true 启用 JWT 认证；默认 false，本地单用户免登录
    CODE_AGENT_ADMIN_USER      初始管理员用户名（默认 admin）
    CODE_AGENT_ADMIN_PASSWORD  认证模式的初始管理员密码（默认 admin123，启用认证时应配置）
    CODE_AGENT_SECRET          JWT 签名密钥（不设则落盘随机密钥）

openai_compat 用于内网网关 / vLLM / Ollama，配合 OPENAI_BASE_URL + OPENAI_API_KEY
即可接入任何 /chat/completions 兼容后端。
"""

from __future__ import annotations

import argparse

import uvicorn

from code_agent.api.app import create_app
from code_agent.config import load_env_file


def main() -> None:
    env_file = load_env_file()  # 先吃 .env，命令行/真实环境变量优先级更高
    parser = argparse.ArgumentParser(prog="code-agent-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--provider",
        default=None,
        choices=["offline", "deepseek", "openai", "openai_compat"],
    )
    args = parser.parse_args()

    app = create_app(provider_name=args.provider)
    if env_file:
        print(f"[code-agent] 已加载 {env_file}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

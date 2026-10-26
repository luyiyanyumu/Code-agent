"""api 包：FastAPI 应用工厂、认证、依赖注入。"""

from code_agent.api.app import build_registry, create_app

__all__ = ["create_app", "build_registry"]

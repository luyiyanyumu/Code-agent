# Code-agent 生产镜像（M7 部署）
# 单阶段即可：纯 Python 依赖，无需编译产物
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CODE_AGENT_HOME=/data

WORKDIR /app

# 先装依赖再拷源码：利用层缓存，改代码不重装依赖
COPY pyproject.toml README.md ./
COPY code_agent ./code_agent
RUN pip install --no-cache-dir . \
    && apt-get update \
    && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

# 4 层防御之最小权限：非 root 运行，数据目录归属该用户
RUN useradd -m -u 10001 agent \
    && mkdir -p /data \
    && chown -R agent:agent /data /app
USER agent

EXPOSE 8765

# 健康检查：/healthz 无需认证
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=3).status==200 else 1)"

CMD ["python", "-m", "code_agent.server", "--host", "0.0.0.0", "--port", "8765"]

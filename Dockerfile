# server-agent 的部署镜像。
# 只装运行依赖，不带开发工具；用非 root 用户运行。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SA_HOST=0.0.0.0 \
    SA_PORT=8000

WORKDIR /app

# 先装依赖（利用层缓存）
COPY pyproject.toml README.md ./
COPY server_agent ./server_agent
COPY evals ./evals
COPY runbooks ./runbooks
COPY knowledge ./knowledge

RUN pip install --no-cache-dir -e . \
    && apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 运行时数据（SQLite / 审计 / trace）
RUN mkdir -p /app/data && chown -R 1000:1000 /app
USER 1000

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["server-agent", "serve"]

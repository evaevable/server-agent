# server-agent 常用命令。`make help` 看全部。
.DEFAULT_GOAL := help
PY := .venv/bin/python
PIP := .venv/bin/pip
SA := .venv/bin/server-agent
BASETEMP := --basetemp=/tmp/sa-pytest

.PHONY: help install test eval dev serve lint clean lab-up lab-down fault-disk fault-cpu fault-nginx fault-port fix-all mcp-list docker-up docker-down

help: ## 显示可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## 创建虚拟环境并安装依赖
	python3 -m venv .venv && $(PIP) install -q -e ".[dev]"

test: ## 跑全部测试（离线，不需要 API Key）
	$(PY) -m pytest -q $(BASETEMP)

eval: ## 跑评测集并输出报告
	$(SA) eval --cases evals/cases --out eval-report.md

dev: ## 启动服务（开发模式，自动重载）
	$(SA) serve

serve: ## 启动服务
	$(SA) serve

lint: ## 语法自检（没装 ruff 时退化为 python -m compileall）
	$(PY) -m compileall -q server_agent evals || true

clean: ## 清理临时数据（不动源码）
	rm -rf data/traces/*.jsonl __pycache__ */__pycache__ */*/__pycache__

lab-up: ## 起靶场（三台靶机）
	cd lab && docker compose up -d --build

lab-down: ## 停靶场
	cd lab && docker compose down -v

fault-disk: ## 在 web-01 注入「磁盘写满」
	docker exec sa-web-01 bash /opt/faults/inject-disk-full.sh

fault-cpu: ## 在 web-01 注入「CPU 打满」
	docker exec sa-web-01 bash /opt/faults/inject-cpu-hog.sh

fault-nginx: ## 在 web-01 注入「nginx 停止」
	docker exec sa-web-01 bash /opt/faults/inject-nginx-down.sh

fault-port: ## 在 web-01 注入「端口冲突」
	docker exec sa-web-01 bash /opt/faults/inject-port-conflict.sh

fix-all: ## 恢复 web-01 的全部故障
	docker exec sa-web-01 bash /opt/faults/fix-all.sh

mcp-list: ## 看我们暴露给 MCP 的工具
	$(SA) mcp list --server "$(PY) -m server_agent.mcp.server"

docker-up: ## 用 compose 起「Agent + 靶机」
	docker compose up -d --build

docker-down: ## 停 compose
	docker compose down -v

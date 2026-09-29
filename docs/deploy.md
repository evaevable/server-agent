# 部署与运维手册

## 三种部署形态

| 形态 | 适合 | 命令 |
|---|---|---|
| 本地运行（最常用） | 单人排障、开发 | `make install && make serve` |
| 容器运行（单机） | 长期跑在一台机器上 | `docker build -t server-agent . && docker run -p 8000:8000 --env-file .env server-agent` |
| Compose（Agent + 靶场） | 演练、演示、培训 | `make docker-up` |

## 必备环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 模型接口（OpenAI 兼容） | 无（必填，除非用 `--mock`） |
| `SA_API_TOKEN` | 远程调用鉴权；**暴露到网络时必须设** | 空（不鉴权） |
| `SA_HOST` | 监听地址 | `127.0.0.1`（容器里设 `0.0.0.0`） |
| `SA_DB_PATH` | SQLite 路径 | `data/server_agent.db` |
| `SA_AUDIT_PATH` | 审计 JSONL | `data/audit.jsonl` |
| `SA_TRACE_DIR` | Trace 目录 | `data/traces` |
| `SA_INVENTORY_PATH` | 主机清单 | `inventory.yaml` |
| `SA_POLICY_ALLOW_PATHS` / `SA_POLICY_ALLOW_SERVICES` | 清理路径 / 重启服务白名单 | `/tmp,/var/tmp`；`nginx,redis,redis-server` |
| `SA_APPROVAL_TIMEOUT` | 审批等待超时（秒），超时=拒绝 | `120` |
| `SA_SANDBOX_BACKEND` | `auto` / `local_docker` / `ags` | `auto` |
| `E2B_DOMAIN` / `E2B_API_KEY` + `AGS_ENABLED=1` | 启用腾讯云 AGS 沙箱 | 未启用 |

## 数据与备份

| 目录/文件 | 内容 | 备份建议 |
|---|---|---|
| `data/server_agent.db` | 会话、任务、事件、事实、主机档案 | 定期 `sqlite3 .backup` 或直接拷贝（低峰期） |
| `data/audit.jsonl` | 审计日志（**安全记录，不要随手删**） | 用 logrotate 按天切分并保留 ≥ 90 天 |
| `data/traces/*.jsonl` | 每次 run 的耗时树 | 可选，按需清理 |

## 升级与回滚

```bash
git pull && make install && make test     # 升级前先跑测试（离线，秒级）
make serve                                # 重启服务
```

回滚：`git checkout <上一个 tag>`（`ch01`…`ch17`），`make install` 后重启。
数据库结构变更目前是「追加式」（`CREATE TABLE IF NOT EXISTS`），旧库可直接用。

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `401` | 未带 Token 或 Token 错 | 检查 `SA_API_TOKEN` 与请求头；`/health` 会返回 `auth: true` |
| `no running event loop` | 在同步上下文里创建任务 | 用 async 路由/函数（第 05 章的坑） |
| 任务一直 `running` | 模型接口卡住或审批未决 | 看 `GET /api/runs/{id}/approvals`；必要时 `POST /api/runs/{id}/cancel` |
| 工具结果被截断 | 结果超过 `max_chars` | 缩小查询范围（加 `port`/`limit`/`grep` 参数） |
| 写操作总被拒 | 审批没人点 / 策略白名单不含该目标 | 看审计 `denied` 记录的 `decision` 字段区分原因 |
| 沙箱报 `Docker daemon 没在运行` | 本地没启动 Docker | `colima start`（macOS）或忽略（只影响 `run_python`） |

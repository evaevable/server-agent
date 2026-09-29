# 上线前安全清单（六项硬检查）

> 这份清单是「能不能把它放到真实环境」的最低门槛。每一项都要**实测**，不要只看代码。

## 1. 网络与鉴权

- [ ] 默认只监听 `127.0.0.1`；若必须暴露，`SA_API_TOKEN` 必须设置且足够长（≥16 字符随机串）
- [ ] 实测：`curl -s -o /dev/null -w "%{http_code}" http://host:8000/api/runs` 返回 **401**
- [ ] 实测：带正确 Token 返回 200/202；带错误 Token 返回 401
- [ ] WebSocket 同样需要 Token（`/ws?token=...`）
- [ ] 如经反向代理：确认 SSE 不被缓冲（`X-Accel-Buffering: no` 已由服务端下发）

## 2. 写操作必须经过审批

- [ ] `SA_POLICY_REQUIRE_APPROVAL=true`
- [ ] `pytest -q tests/test_policy.py` 全绿（含注入攻击用例）
- [ ] 实测：让 Agent 重启一个白名单内服务，前端/终端必须出现审批请求；**拒绝后确认没有执行**
- [ ] 实测：`server-agent tools call restart_service '{"name":"nginx"}' --no-approval` 退出码非 0 且未执行

## 3. 授权范围最小化

- [ ] `inventory.yaml` 里每台机器的 `allowed_paths` 不含 `/`、`/etc`、`/usr`、`/var/lib`
- [ ] `allowed_services` 只列真正需要重启的服务
- [ ] `SA_POLICY_ALLOW_PATHS` 不含 `/var/log` 之外的敏感目录（按业务决定）
- [ ] 密码用 `password_env`，清单里没有明文密码
- [ ] 真机环境把 `strict_host_key` 设为 `true`

## 4. 审计可查、不可抵赖

- [ ] `SA_AUDIT_ENABLED=true`，`data/audit.jsonl` 有写入
- [ ] `server-agent audit -v` 能看到：`tool_call` / `approval` / `approved` / `denied` / `tool_result`
- [ ] 审计里**没有明文密钥**：`grep -iE "password=|api_key=|sk-" data/audit.jsonl` 应无命中（或只有 `***`）
- [ ] 配置了日志轮转与保留期（≥90 天）

## 5. 秘密与沙箱

- [ ] 沙箱里不放任何目标机凭证（检查 `run_python` 的输入只有数据、没有连接串）
- [ ] 工具输出经过脱敏（`redact`）：构造一个含 `password=xxx` 的文件，用 `tail_file` 读取，确认输出是 `***`
- [ ] 模型侧：确认 `LLM_API_KEY` 不在前端、不在日志、不在仓库里（`.env` 已 gitignore）

## 6. 停得下来（最容易被忽略）

- [ ] 任务可以取消：`POST /api/runs/{id}/cancel` 实测有效
- [ ] 审批可以拒绝，且拒绝后任务继续用「未执行」的事实给结论
- [ ] 服务可以停止：知道进程在哪、怎么停（`pkill -f "server-agent serve"` 或容器 stop）
- [ ] 误操作可回滚：至少演练过一次「清理后怎么恢复」（备份/快照）
- [ ] 知道在紧急情况下如何**临时禁用写操作**：`SA_POLICY_ALLOW_SERVICES=`（空）+ `SA_POLICY_ALLOW_PATHS=/tmp` 并重启

---

## 上线后建议持续做

| 频率 | 动作 |
|---|---|
| 每次改动 | `make test`（离线逻辑回归）+ `make eval`（评测基线） |
| 每周 | 看审计里的 `denied` 记录：是模型乱试，还是白名单配得太紧？ |
| 每月 | 复查白名单与令牌；清理过期 trace |
| 每次故障复盘 | 把真实故障补进 `evals/cases/`（**这是评测集持续变强的唯一方式**） |

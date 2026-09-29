# 附录 C 排障速查：Agent 用的命令与怎么看

## 本机排查（Agent 工具 ↔ 传统命令）

| 想知道 | 工具 | 等价命令 |
|---|---|---|
| 这台机器什么情况 | `host_info` | `uname -a; uptime; nproc` |
| CPU / 内存紧不紧 | `cpu_memory_usage` | `top -bn1 \| head; free -h; uptime` |
| 哪个分区满了 | `disk_usage` | `df -h` |
| 谁在吃 CPU / 内存 | `top_processes` | `ps aux --sort=-%cpu \| head` |
| 端口被谁占着 | `listening_ports` | `ss -lntp`（macOS：`lsof -nP -iTCP -sTCP:LISTEN`） |
| 日志最近报了什么 | `tail_file` | `tail -n 100 app.log`；`grep -i error` |
| 目录为什么大 | `run_command` | `du -sh /var/log/* \| sort -h \| tail` |

## 远程与容器

| 场景 | 命令 |
|---|---|
| 远端只读排查 | `remote_run(host, "df -h")` → `ssh host df -h` |
| 远端日志 | `remote_logs(host, path, grep=...)` → `ssh host tail -n 50 path` |
| 靶场起停 | `make lab-up` / `make lab-down` |
| 注入故障 | `make fault-disk` / `fault-cpu` / `fault-nginx` / `fault-port` |
| 恢复 | `make fix-all` |
| 进容器看 | `docker exec -it sa-web-01 bash` |

## 平台自身的排查

| 想知道 | 命令 |
|---|---|
| 服务活着吗 | `curl -s localhost:8000/health` |
| 任务卡在哪 | `curl -s localhost:8000/api/runs/{id} \| jq` |
| 有没有待审批 | `curl -s localhost:8000/api/runs/{id}/approvals \| jq` |
| 谁做了什么 | `server-agent audit -v --limit 30` |
| 时间花在哪 | `jq -c '{name, duration_ms}' data/traces/<run_id>.jsonl` |
| 历史结论 | `server-agent history --limit 10` |
| 表现好不好 | `server-agent eval --cases evals/cases` |
| 跑测试 | `make test` |

## 常见故障速判

| 现象 | 先看什么 | 常见根因 |
|---|---|---|
| 磁盘满 | `df -h` → `du -sh /var/log/*` | 日志未轮转、容器日志无上限、被删除但未释放的文件 |
| CPU 高 | `uptime` 看 load → `ps --sort=-%cpu` | 死循环、批处理任务重叠、外部流量 |
| 服务起不来 | `ss -lntp` 看端口 → `systemctl status` | 端口被占、配置语法错、依赖连不上 |
| 502 | `ss -lntp` → `tail error.log` | 后端没起来、上游超时、配置未加载 |
| 内存不足 | `free -h` → `ps --sort=-%mem` | 内存泄漏、缓存未限、OOM 后服务被 kill |

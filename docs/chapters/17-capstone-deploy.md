# 第 17 章 总演习：故障靶场实战与交付部署

**前置知识**：全部。这一章不引入新概念，只做两件事：**端到端跑一遍**、**把它交付出去**

**本章代码**：`Dockerfile`、`docker-compose.yml`、`Makefile`、`docs/deploy.md`、`docs/security-checklist.md`。

**学习目标**：读完本章，应能回答以下问题。

1. 一次真实排障里各模块怎么协作？
2. 上线前必须检查什么？
3. 下一步往哪走？

---

## 17.1 端到端演练剧本

**场景**：`web-01` 上被注入了两个故障——磁盘写满 + nginx 停止；用户只说了一句「web-01 怎么访问不了了」。

```mermaid
sequenceDiagram
    participant U as 用户
    participant A as Agent
    participant T as 工具/执行器
    participant L as 靶机 web-01
    U->>A: web-01 怎么访问不了了
    A->>T: listening_ports(host=web-01)
    T->>L: ssh: ss -lntp
    L-->>T: 只有 22 端口
    A->>T: remote_run("df -h") / remote_logs(error.log)
    T->>L: ssh: df -h；tail error.log
    L-->>T: / 使用率 95%；日志 connect() failed
    A->>T: search_knowledge("端口未监听 502")
    A->>T: load_runbook("disk-full")
    Note over A: 生成诊断报告：根因=磁盘写满导致 nginx 被 OOM/写失败停掉
    A->>U: 申请审批：清理 /var/log/fill 下 7 天前的文件 + 重启 nginx
    U-->>A: 批准
    A->>T: clean_directory / remote_restart_service
    T->>L: 执行
    A->>T: 复查（df + ss）
    A-->>U: 结论 + 证据链 + 已执行动作
```

**跑法（需要 Docker）**：

```bash
make lab-up                     # 起三台靶机
export LAB_SSH_PASSWORD=labpass
make fault-disk fault-nginx     # 注入两个故障（docker exec 到 web-01）
server-agent ask --plan "web-01 怎么访问不了了？帮我查清楚并处理"
#   交互式审批：清理与重启会各问一次 y/N
make fix-all                    # 恢复
make lab-down
```

---

## 17.2 这一步真正在验证什么

跑完这一遍，你验证的其实是**前面 16 章是否真的能拼起来**：

| 环节 | 来自哪一章 | 出问题的表现 |
|---|---|---|
| 循环、步数上限、错误回喂 | 04 | 卡在同一个工具上反复查 |
| 工具与结果上限 | 03 | 报告里出现被截断的 JSON 碎片 |
| 远程执行与主机授权 | 10 | 「未授权重启该服务」「清单里没有这台主机」 |
| 策略与审批 | 09 | 没经过审批就执行了写操作（严重） |
| 手册与文档 | 12/13 | 结论里没有引用来源 |
| 上下文压缩 | 08 | 报告明显丢失早期观察（如端口信息） |
| 报告结构 | 07 | 前端显示「未解析为结构化报告」 |
| 评测 | 15 | 同一场景在评测里过、在真机上不过（说明桩与真实差异大） |
| Trace | 15 | 查不到时间花在哪 |

**这也是为什么最后要做一章演习**：单元测试全绿不代表系统能用；**只有端到端跑过，你才知道哪两个模块的接口理解不一致。**

---

## 17.3 交付：三条命令起服务

```bash
# 1. 本地开发
make install && make test          # 离线，不需要 Key

# 2. 起服务（终端）
SA_API_TOKEN=devtoken LLM_BASE_URL=... LLM_MODEL=... make serve
#   浏览器打开 http://127.0.0.1:8000

# 3. 全套（Agent + 三台靶机）
export SA_API_TOKEN=devtoken LAB_SSH_PASSWORD=labpass
export LLM_BASE_URL=... LLM_API_KEY=... LLM_MODEL=...
make docker-up
```

容器化时的四个细节（都在 `Dockerfile` / `docker-compose.yml` 里）：

| 细节 | 原因 |
|---|---|
| 非 root 用户（uid 1000） | 容器逃逸的收益从「root」降为「普通用户」 |
| `data/` 挂卷 | SQLite / 审计 / trace 要持久化，不能随容器销毁 |
| `HEALTHCHECK` 打 `/health` | 编排系统据此判断存活（第 5 章的 liveness 思路） |
| 靶机与 Agent 同网络、清单用服务名 | 容器里用 `web-01` 而非宿主机的 `127.0.0.1:2201` |

---

## 17.4 上线前的安全清单

六条硬检查（详见 [docs/security-checklist.md](../security-checklist.md)）：

| 检查 | 命令 / 动作 |
|---|---|
| 1. 只监听本机或已设 Token | `SA_HOST=127.0.0.1` 或必设 `SA_API_TOKEN`；验证 `curl` 无 Token 返回 401 |
| 2. 写操作都要审批 | 跑 `pytest -q tests/test_policy.py`（含注入用例）与评测里的 `min_denials` 用例 |
| 3. 授权范围按主机写死 | 检查 `inventory.yaml` 的 `allowed_paths` / `allowed_services`，不含 `/`、`/etc` |
| 4. 审计开启且能查 | `SA_AUDIT_ENABLED=true`；`server-agent audit -v` 能看到审批与拒绝记录 |
| 5. 秘密不进沙箱、不进日志 | `redact` 生效；`grep -c '\*\*\*' data/audit.jsonl`；沙箱无凭证 |
| 6. 有回滚与 kill switch | 知道怎么停服务、怎么撤销一次误操作（靶场先演练一遍） |

**最容易被忽略的是第 6 条**：能执行变更的 Agent，必须配一个「怎么停下来」的答案。最低限度是：任务可取消（第 5 章）、审批可拒绝（第 9 章）、服务可停止、数据可回滚。

---

### 生产加固：从「能演示」到「能上线」

靶场里能跑通，不等于能放到真实机器上。交付前还做了下面几项加固，每一项都对应一个线上才会遇到的问题：

| 问题 | 加固 | 位置 |
|---|---|---|
| `df` 显示满，`du` 却统计不出来 | `deleted_open_files`：从 `/proc/*/fd` 找「已删除但仍被进程打开」的文件（≈ `lsof +L1`） | `tools/linux.py` |
| `du` 一层层往下钻太慢 | `find_large_files`：不跨文件系统、不跟符号链接，带 8 秒与 30 万条目预算，超时返回部分结果 | `tools/linux.py` |
| 空间还有但写不进文件 | `disk_usage` 增加 inode 使用率 | `tools/system.py` |
| 服务是「没起来」还是「反复崩溃」 | `service_status`：`systemctl show` 的重启次数、退出码 + 最近 journal | `tools/linux.py` |
| 重启命令返回 0，服务其实没起来 | `restart_service` 以 systemd 为主，重启后用 `is-active` 确认，失败附最近 journal | `tools/ops.py` |
| 日志工具被诱导去读私钥 | `tail_file` 拒绝凭证/私钥/`.env`/`~/.ssh` 等路径（原始、normpath、resolve 三种形态都检查），内容再脱敏；`remote_logs` 白名单改为按目录边界比较，防 `..` 穿越 | `policy/risk.py` |
| 服务重启时正在跑的任务「凭空消失」 | FastAPI `lifespan` 退出时调用 `RunManager.shutdown()`：取消在跑任务、落库为 cancelled、撤销挂起审批 | `agent/runs.py` |
| 线上排查只能翻终端输出 | `SA_LOG_FORMAT=json`：每行一个 JSON，带 `run_id`、`tool`、`duration_ms` | `logging_setup.py` |

这几项的共同点是：**测试环境里几乎碰不到，线上第一周一定会碰到**。它们都配了离线回归测试（`tests/test_linux_tools.py`、`tests/test_robustness.py`、`tests/test_policy.py`），/proc 与 systemctl 都用替身模拟，在 macOS 上也能跑。

---

## 17.5 下一步可以往哪走

按「先补短板，再扩能力」的顺序：

| 方向 | 触发信号（什么时候该做） |
|---|---|
| 定时巡检 | 你已经在手工重复同一批检查 |
| 接入告警 | 你希望它比人先看到故障（这时要加去重与静默，否则会被告警刷屏） |
| 更细的权限 | 多人使用：需要按人/按角色区分「谁能批准什么」 |
| 向量检索 | 评测显示 BM25 漏掉了大量「词不匹配」的查询 |
| 并行多机 | 一次要检查 10+ 台机器（此时要加并发上限与汇总策略） |
| 真正的审批体系 | 需要留痕与合规（对接你们的工单/审批系统） |

**一条提醒**：本项目的每个能力都配了评测与边界说明，这是它能长期演进的前提。**没有评测的能力扩张，最后都会变成没人敢用的黑盒。**

---

## 17.6 代码走读

```text
Dockerfile              # 非 root + healthcheck + data 挂卷
docker-compose.yml      # agent + web-01/web-02/db-01（一键起全套）
inventory.docker.yaml   # 容器内清单（用服务名与容器端口）
Makefile                # install/test/eval/serve/lab-up/fault-*/fix-all/mcp-list/docker-up
docs/deploy.md          # 部署与运维手册
docs/security-checklist.md  # 上线前六项硬检查
```

---

## 17.7 动手练习

1. **完整演习**（需要 Docker）：按17.1 节 的剧本跑一遍，记录每一步的实际现象。
2. **制造一次「Agent 被拒绝」**：清理操作时在终端输 `n`，观察它是否停止、报告里怎么写这件事。
3. **制造一次「Agent 想越权」**：让它重启 `db-01` 上的 `nginx`（不在它白名单里），确认被拒。
4. **交付演练**：`make docker-up`，然后从浏览器完成一次排查（前端有审批卡片）。
5. **思考题**：如果要让这个 Agent 接入你们的告警系统自动处理「磁盘满」，你会怎么设计安全边界？（提示：白名单 + 审批 + 审计之外，还需要「同时处理几条」「失败几次就停」这类策略。）

---

## 17.8 验收清单

```bash
make test                        # 全部离线测试通过
make eval                        # 评测报告：通过率 100%
make lab-up && make fault-disk   # 需要 Docker
server-agent ask --plan "web-01 磁盘满了吗"
make fix-all && make lab-down
make docker-up                   # 全套容器化（需要 Docker + compose 插件）
```

---

## 17.9 本章小结

**要点**
1. 端到端演习的价值在于**暴露模块之间的接口误解**——单元测试全绿不等于系统能用。
2. 交付只要三条命令（install/test/serve 或 docker-up），但容器化要注意非 root、数据挂卷、健康检查、容器内清单。
3. 上线前有六项硬检查，其中最容易漏的是「**怎么停下来**」：能改系统的 Agent 必须配好取消、拒绝、停止与回滚。

**自测题**（括号内为对应小节）
1. 端到端演习能验证哪些环节？（17.2 节）
2. 容器化时要特别注意哪四点？（17.3 节）
3. 上线前六项检查里，哪一项最容易被忽略？为什么它重要？（17.4 节）
4. 什么信号说明「该做定时巡检」而不是继续手工查？（17.5 节）
5. 为什么说「没有评测的能力扩张会变成没人敢用的黑盒」？（17.5 节）

---

## 结语

到这里，17 章结束了。回头看你拥有的东西：

- 一个**能跑、能远程调用、有前端、能在靶场真的修好故障**的运维 Agent；
- 一套**讲得清为什么**的模块文档与架构决策记录；
- 一把**能证明它靠谱**的评测尺子（离线可跑、进 CI）；
- 以及最重要的一件事：**遇到任何 Agent 需求时，你知道从哪一层开始设计与担心。**

下一步的建议很简单：**把它接到你真实的一台测试机上，用你自己的故障去跑。** 那才是真正的考试。

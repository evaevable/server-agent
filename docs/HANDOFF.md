# 新会话交接提示词（复制即用）

> 用途：在**新会话**里继续 server-agent 的开发/学习。第一次在新会话里发送「通用交接提示词」，之后正常对话即可。

---

## 通用交接提示词（推荐，直接整段复制）

```text
我在继续「以练带学」的 Agent 课程项目：server-agent。请先恢复上下文再动手。

【项目位置与状态】
- 本地仓库：/Users/lanceche/WorkBuddy/2026-09-29-10-41-08/server-agent
- 远程：git@github.com:evaevable/server-agent.git（push 必须用 SSH；https 形式的 git 协议在本机被拦）
- 进度：17 章 + 3 附录全部已发布（tag ch01…ch09、ch10-ch17），README 课程表全绿
- 测试基线：.venv/bin/pytest -q --basetemp=/tmp/sa-pytest → 292 passed, 1 skipped（Docker 不可用时跳过）
- 评测基线：.venv/bin/server-agent eval --cases evals/cases → 通过率 100%

【请先做这三件事，再回答我】
1. 加载 skill：server-agent-course-tutor（内含课程表、交付规范、踩坑铁律与当前进度）
2. 读 docs/SYLLABUS.md + README.md，确认章节与模块现状
3. 跑 .venv/bin/pytest -q --basetemp=/tmp/sa-pytest 确认基线是绿的

【已知能力（不用重新实现）】
ReAct 循环与事件流；18 个工具（含远程/沙箱/记忆/知识/Runbook）；FastAPI + SSE + WebSocket + 前端控制台；
策略层（风险分级 / 人工审批 / 审计 / 脱敏）；SQLite 记忆与主机档案；SSH 多主机执行器 + 主机清单；
Docker 与腾讯云 AGS 沙箱（run_python）；Plan-and-Execute 与 Runbook；BM25 知识库检索（带引用溯源）；
MCP Server/Client；Trace（耗时树）+ 离线评测集；多 Agent（诊断/执行/审查）；Docker Compose 一键交付。

【本机限制（别误判为 bug）】
- Docker daemon 未启动（colima 未运行、docker compose 插件也缺）→ 靶场/SSH 路径未实机验证
- 没有真实模型 API Key → 涉及真模型的验证要我用 .env 配置后自己跑

【我这次要做的】
<在这里写清楚，例如：扩展定时巡检 / 接入我的一台真机 / 重讲第 9 章 / 修某个 bug>

【硬性要求】
- 改代码前先跑测试；改完 make test 必须全绿，且 make eval 保持 5/5
- 新增模块要写 docs/modules/<module>.md；有重大取舍写 docs/adr/
- 讲义遵守课程规范：导读 → 积木 → 代码走读 → 动手练习 → 验收清单 → 小结与自测题 → 下一章预告
- 提交后核对 git rev-parse HEAD 与 origin/main 一致
```

---

## 场景化版本（按需替换最后一段）

### A. 扩展功能（例如定时巡检 / 接入告警）

```text
【我这次要做的】
给 server-agent 增加「定时巡检」能力：
- 每 N 分钟对 inventory 里的主机跑一批只读检查（磁盘/负载/端口/日志关键字）
- 结果异常时才产出报告，走现有的报告结构 + 通知（先用 CLI 打印，通知渠道留接口）
- 必须有开关、有审计、不引入新的写操作
请先给我一个设计方案（数据流、配置项、复用哪些模块），我确认后再动手。
```

### B. 接入真实机器

```text
【我这次要做的】
我要把它接到一台真实测试机（不是靶场）：
- 机器：<IP/主机名>，账号：<user>，认证：<密码或密钥路径>
- 只允许只读排查 + 重启 <服务名>，其他写操作一律禁止
- 请帮我改 inventory.yaml（含 allowed_paths/allowed_services 最小化）、给出验证步骤，
  并逐条对照 docs/security-checklist.md 的六项硬检查告诉我哪些还没过。
```

### C. 复盘某一章 / 继续教学

```text
【我这次要做的】
重讲第 <N> 章，并按课程规范补充：更贴近我工作的例子、以及 3-5 道自测题。
讲义更新到 docs/chapters/<对应文件>，更新后按规范提交推送。
```

### D. 修 bug

```text
【我这次要做的】
现象：<贴报错或描述>
复现步骤：<命令>
期望：<应该怎样>
请先定位根因（不要只改表象），给出最小修改方案，补一个回归测试再提交。
```

---

## 最短版（懒人版，够用就行）

```text
继续 server-agent 项目：本地 /Users/lanceche/WorkBuddy/2026-09-29-10-41-08/server-agent，
先加载 skill server-agent-course-tutor 并跑 make test 确认基线（期望 292 passed, 1 skipped），
然后我要：<一句话需求>
```

---

## 小贴士

| 事项 | 说明 |
|---|---|
| 会话不必带历史 | 所有进度、规范、踩坑都写在仓库与 skill 里，新会话读一遍即可 |
| 别忘了本机限制 | Docker 未启动、无模型 Key；先说清就能省掉一轮试错 |
| 想省钱地验证 | 用 `--mock`（离线）或 `server-agent eval`（评测集），都不花 token |
| 交付前自检 | `make test` → `make eval` → `git rev-parse HEAD` 与 `origin/main` 比对 |

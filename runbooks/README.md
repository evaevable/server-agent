# runbooks：排障手册

> 引入章节：第 12 章（规划、反思与 Runbook）· 加载代码：`server_agent/knowledge/runbooks.py` · 模块文档：[docs/modules/planner.md](../docs/modules/planner.md)

Runbook 是「专家会怎么查」的**程序性知识**：遇到某类故障时先看什么、再看什么、什么情况下才动手。
Agent 通过两个只读工具使用它们：

| 工具 | 作用 |
|---|---|
| `list_runbooks(question, limit)` | 按问题描述匹配 `symptoms`，返回最相关的几本（名字、标题、症状，不含正文） |
| `load_runbook(name)` | 加载一本手册的完整步骤，作为接下来排查的路线 |

## 现有手册

| 文件 | name | 适用症状 |
|---|---|---|
| [disk-full.md](disk-full.md) | `disk-full` | 磁盘使用率过高、写满、`no space left` |
| [cpu-high.md](cpu-high.md) | `cpu-high` | CPU 或负载过高、机器卡顿 |
| [service-502.md](service-502.md) | `service-502` | 502 / 503、服务起不来、upstream 连不上 |
| [port-conflict.md](port-conflict.md) | `port-conflict` | 端口被占用、`address already in use` |

## 文件格式

每本手册是一个 Markdown 文件，**必须以 YAML frontmatter 开头**（第一行就是 `---`）：

```markdown
---
name: disk-full                 # 唯一标识，load_runbook 用它；省略时取文件名
title: 磁盘使用率过高 / 写满        # 给人和模型看的标题
symptoms: [磁盘, 满了, disk, full, no space]   # 匹配关键词（大小写不敏感，子串匹配）
---
# 磁盘满排查手册

## 判断
## 定位
## 处置（写操作，需要审批）
## 验证
```

- 加载规则：目录下所有 `*.md` 中，**只有以 `---` 开头的文件才算手册**。本 README 没有 frontmatter，所以不会被当成手册加载。
- 匹配规则：`symptoms` 里有几个词出现在问题描述里就得几分，按分数取前 `limit` 本；
  一本都没命中（或 `question` 为空）时，退回列出全部手册的前 `limit` 本，让模型自己挑。
  没有语义理解，所以关键词要**中英文、口语和术语都写上**（如「卡」「卡顿」「load」）。
- 正文建议固定成「判断 → 定位 → 处置 → 验证」四段。处置段只描述要做什么，
  真正执行仍然走写操作工具 + 策略层 + 人工审批（[ADR-0002](../docs/adr/0002-no-arbitrary-shell.md)），手册不是绕过审批的通道。

## 新增一本手册

1. 在本目录新建 `<slug>.md`，写好 frontmatter 和四段正文；
2. 自查：`server-agent tools call list_runbooks '{"question": "<一句典型的用户描述>"}'`，能命中新手册；
3. 自查：`server-agent tools call load_runbook '{"name": "<slug>"}'`，能拿到正文；
4. 如果是仓库自带手册，把它加进上面的表格，并在 `tests/test_planning_runbooks.py::test_repo_runbooks_load` 的期望集合里补上名字。

## 目录解析

手册目录先读配置 `SA_RUNBOOKS_DIR`（默认 `runbooks`，相对当前工作目录）；不存在时回退到仓库根的 `runbooks/`。
这样测试 chdir 到临时目录、或服务从别的目录启动时都能找到手册。Docker 镜像里通过 `COPY runbooks ./runbooks` 带进去。

## 和知识库（第 13 章）的区别

| | Runbook | 知识库 `knowledge/` |
|---|---|---|
| 内容 | 排查流程（怎么查） | 事实与历史（部署目录、复盘记录） |
| 来源 | 自己写、随仓库评审 | 可能来自别人、可能过期 |
| 检索 | `symptoms` 关键词匹配，整本加载 | BM25 切块检索，带引用 |

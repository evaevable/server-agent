# 第 12 章 先想后做：规划、反思与 Runbook

**前置知识**：第 4 章（ReAct）、第 7 章（结构化输出）、第 10 章（靶场）

**本章代码**：`server_agent/agent/planner.py`、`server_agent/knowledge/runbooks.py`、`runbooks/*.md`。

**学习目标**：读完本章，应能回答以下问题。

1. ReAct 什么时候不够用？
2. 计划该怎么表示和推进？
3. Runbook 和提示词、RAG 的区别？

---

## 12.1 ReAct 的三种失效模式

第 4 章的 ReAct 是「走一步看一步」。它在简单问题上很好，但复杂排查会暴露三种毛病：

| 失效模式 | 现象 | 后果 |
|---|---|---|
| **绕圈** | 反复查同类信息（磁盘、磁盘、磁盘） | 浪费步数与 token |
| **漏项** | 忘了查内存、忘了看日志 | 结论片面 |
| **无全局视野** | 用户只看到它东一榔头西一棒子 | 不信任、也无法判断是否快结束 |

Plan-and-Execute 的修法很直接：**先花一次模型调用把计划写出来**，再按计划推进。

```mermaid
flowchart LR
    Q["用户问题"] --> P["生成计划（一次调用）"]
    P --> S["把计划写进提示词"]
    S --> L["ReAct 循环按计划推进"]
    L -->|"某步失败"| R{"重规划?"}
    R -->|"需要"| P
    R -->|"不需要"| C["给结论"]
    L --> C
```

| 收益 | 代价 |
|---|---|
| 有计划、可展示（`plan` 事件）、可对齐专家思路 | 多一次模型调用（几分钱、1-2 秒） |
| 每步有状态（pending/done/failed），用户看得懂进度 | 计划可能一开始就不对 → 必须允许调整 |
| 失败时能定位「是哪一步不行」 | 提示词变长（计划文本） |

**什么时候值得开？** 复杂、多步、需要多方核实的排查值得；「80 端口被谁占了」这种一步就能答的不值得。所以本章把它做成**开关**（`SA_AGENT_PLANNING` / `--plan`），而不是默认行为。

---

## 12.2 计划的数据结构：状态比文字更重要

计划不是一段文字，而是一个**带状态的列表**：

```python
PlanStep(goal="看 CPU 与内存使用率", tool_hint="cpu_memory_usage", status="pending")
Plan(rationale="先看整体资源，再定位到进程，最后给结论", steps=[...], replans=0)
```

| 字段 | 作用 |
|---|---|
| `goal` | 这一步要达成什么（可验证） |
| `tool_hint` | 建议用什么工具（给模型提示，不是命令） |
| `status` | `pending` / `done` / `failed` / `skipped`，供前端展示与循环推进 |
| `rationale` | 一句话思路，让用户知道「它打算怎么查」 |

推进规则（简单但够用）：

| 情况 | 动作 |
|---|---|
| 工具调用成功 | `mark_next_done()`：第一个未完成步骤标记 done |
| 整轮工具调用全失败 | `mark_last_failed()`：当前步骤标记 failed，模型可自行调整 |
| 计划生成失败（输出不是 JSON） | **不阻塞**：退化为纯 ReAct，继续排查 |

最后一条是本章最重要的工程判断：**计划是增强，不是前置条件**。计划生成失败、解析失败、模型不遵守计划，都不该让排查停下来——所以要允许「无计划运行」。

### 一个高频误解：「有计划就一定比没计划好」

不一定。计划的收益来自「问题足够复杂」，代价是「多一次调用 + 可能误导」。一个三步就结束的问题，先花一次调用生成计划纯属浪费；而一个**错误的计划**比没有计划更糟——它会把模型锚定在错的方向上。所以：

1. 用开关控制，而不是默认全开；
2. 计划里写清楚「如某步受阻，说明原因并调整计划，不要硬凑」；
3. 第 15 章的评测用同一批故障对比「开/关计划」的步数、命中率与成本——**用数据决定默认值，而不是拍脑袋**。

---

## 12.3 Runbook：把「怎么查」写成手册

提示词是**思维方式**（先宏观后微观、证据链），Runbook 是**具体流程**（磁盘满：先 `df`，再 `du`，再看轮转）。

| 维度 | 系统提示词 | Runbook | RAG（第 13 章） |
|---|---|---|---|
| 内容 | 通用方法论 | 具体故障的处理流程 | 任意文档集合 |
| 可信度 | 自己写的，可信 | 自己写的，可信 | 别人写的，可能过期 |
| 加载方式 | 每次都在 | **按症状检索后加载** | 检索 + 引用 |
| 长度 | 3-4k 字符 | 每本 1-2k 字符 | 不定 |

Runbook 用 Markdown + frontmatter，关键是 `symptoms` 字段：

```markdown
---
name: disk-full
title: 磁盘使用率过高 / 写满
symptoms: [磁盘, 满了, 空间不足, disk, full, no space, df, 使用率]
---
# 磁盘满排查手册
## 判断 ...
## 定位 ...
## 处置（写操作，需要审批）...
## 验证 ...
```

配套两个工具：

| 工具 | 作用 | 为什么要两个 |
|---|---|---|
| `list_runbooks(question)` | 按症状匹配，返回手册名与标题（**不含正文**） | 先让模型知道「有哪几本可用」，成本低 |
| `load_runbook(name)` | 加载某一本的完整步骤 | 只加载命中的那本，避免把全部手册塞进上下文 |

**为什么不把手册直接塞进系统提示词？** 四本手册加起来几千字，每次都花这份 token；更糟的是**无关手册会干扰判断**（模型可能硬套「磁盘满」的流程去处理一个端口冲突问题）。按需加载才是对的。

四本手册随仓库提供：`disk-full` / `cpu-high` / `service-502` / `port-conflict`——它们和第 10 章靶场的四个故障注入脚本一一对应。

---

## 12.4 代码走读

```text
server_agent/agent/planner.py        # PlanStep / Plan / Planner（生成 + 解析 + 状态推进）
server_agent/knowledge/runbooks.py   # Runbook / RunbookLibrary（frontmatter 解析、症状匹配、目录加载）
server_agent/tools/runbook_tools.py  # list_runbooks / load_runbook
runbooks/*.md                        # 四本手册（与靶场故障一一对应）
server_agent/agent/loop.py           # planning=True：生成计划 → 写进提示词 → 每步推进 → 发 plan 事件
server_agent/cli.py                  # --plan 开关；终端渲染计划清单
```

### 常见陷阱：两个坑

**坑 1：`str.format` 又一次被 JSON 示例的花括号搞崩。** 计划模板里有一段示例 JSON `{"steps": [...]}`，如果用 `PLAN_INSTRUCTION.format(question=...)`，于是 `format` 把 `"steps"` 当成字段名 → `KeyError: '"steps"'`。

这和**第 7 章踩的是同一个坑**（当时是 `Template.substitute` 少传占位符）。教训升级为一条规则：**任何包含示例代码/JSON 的模板，一律用 `string.Template` 的 `$var`，不要用 `str.format` 或 f-string。**

**坑 2：测试里「失败标记落在了错的那一步」。** 若按 `reversed(steps)` 找待标记的步骤，结果标记到了**最后一步**；但计划是按顺序推进的，正在做的是**第一个 pending**。这不是测试写错，是**语义写错**——测试恰好抓住了它。教训：**「第一个」和「最后一个」在状态机里差别巨大，写的时候要问清楚「谁才是当前的」。**

---

## 12.5 动手练习

1. **看计划长什么样**（离线可跑）：
   ```bash
   pytest -q tests/test_planning_runbooks.py -k plan -s
   server-agent ask --plan --mock "这台机器为什么卡"     # 观察 stderr 里的 [计划] 段落
   ```
2. **让 Runbook 上场**：
   ```bash
   server-agent tools call list_runbooks '{"question": "web-01 磁盘快满了"}'
   server-agent tools call load_runbook '{"name": "disk-full"}'
   ```
3. **写一本自己的手册**：在 `runbooks/` 下加一本（比如 `redis-slow`），写好 `symptoms`，然后问一个相关问题，看它能不能被匹配到。
4. **对照实验**（配好模型）：同一个问题分别用 `--plan` 与不带 `--plan` 各跑一次，比较步数、token、结论质量。这就是第 15 章评测要自动做的事。
5. **思考题**：现在计划是「一次性生成」的。如果第 2 步的观察推翻了第 1 步的假设，理想行为是什么？（提示：重规划会不会造成死循环？怎么限制重规划次数？）

---

## 12.6 验收清单

```bash
pytest -q tests/test_planning_runbooks.py        # 15 passed
server-agent ask --plan --mock "机器很卡"         # 有 [计划] 输出
server-agent tools call list_runbooks '{"question": "502"}'
server-agent tools call load_runbook '{"name": "service-502"}'
```

---

## 12.7 本章小结

**要点**
1. ReAct 的失效模式是绕圈、漏项、没有全局视野；Plan-and-Execute 用「先出计划、再推进、可调整」来解决，代价是多一次调用。
2. 计划是**带状态的列表**（pending/done/failed），状态比文字更重要；**计划生成失败必须能降级为无计划运行**。
3. Runbook 是团队的「程序性知识」：按症状检索、按需加载、只加载命中的那本——不要把所有手册塞进提示词。

**自测题**（括号内为对应小节）
1. ReAct 的三种失效模式分别是什么？（12.1 节）
2. Plan-and-Execute 的收益与代价？（12.1 节）
3. 为什么计划要用「带状态的数据结构」而不是一段文字？（12.2 节）
4. 「计划生成失败时降级为无计划」为什么很重要？（12.2 节）
5. Runbook、提示词、RAG 三者在可信度与加载方式上有什么区别？（12.3 节）
6. 为什么 `list_runbooks` 只返回标题不返回正文？（12.3 节）
7. 本章两个坑的共同点是什么？由此得到哪条模板使用规则？（常见陷阱）

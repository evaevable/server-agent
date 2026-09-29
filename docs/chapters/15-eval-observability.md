# 第 15 章 先有尺子：评估与可观测性

> **本章导读**
> - 建议用时：知识 45 分钟 + 动手 30 分钟
> - 前置知识：第 04 章（事件流）、第 09 章（审计）、第 12/13 章的「要不要开计划 / 要不要上向量」
> - 读完能回答：Agent 评测难在哪？怎么做到可重复？Trace 能回答什么问题？指标怎么选？
> - 本章代码：`server_agent/tracing/`、`evals/`、`server-agent eval`。对应 tag `ch15`。

---

## 【积木 15-1】Agent 评测为什么比传统软件难

| 传统软件的测试 | Agent 的评测 |
|---|---|
| 输入确定 → 输出确定 | 同一个问题，每次路径都可能不同 |
| 判定是「等于/不等于」 | 判定是「结论对不对、有没有证据、有没有越界」 |
| 失败可复现 | 依赖模型版本、温度、上下文长度 |

三条应对策略（本章全用了）：

| 策略 | 做法 |
|---|---|
| **把模型换成脚本** | 用 MockLLM 回放固定行为 → 评测的是 **Agent 逻辑**，不是模型的随机性 |
| **把机器换成桩** | 桩工具返回固定数据（磁盘 95%、load 9.2）→ 不受真机状态影响，可回归 |
| **把判定写成规则** | 根因关键词、必须调用的工具、步数上限、写操作是否被拦（可自动判定） |

还有一条容易被忽略：**「不乱动手」也要被测**。本章专门有一条用例（`no-tool-fabrication`）检查模型在没采集任何数据时**不要调用写操作**。

---

## 【积木 15-2】评测集长什么样

一条用例 = 问题 + 机器状态 + 模型脚本 + 期望：

```yaml
- id: disk-full-happy
  question: web-01 磁盘是不是快满了？帮我查一下原因
  scenario: disk_full                # 桩工具返回「磁盘 95%」
  mock:
    - tool_call: {name: disk_usage, arguments: {path: "/"}}
    - tool_call: {name: tail_file, arguments: {path: "/var/log/nginx/error.log", lines: 50}}
    - report: {summary: ..., root_cause: nginx 日志未轮转, ...}
  expect:
    stopped: final
    max_steps: 4
    tools_called: [disk_usage, tail_file]
    root_cause_contains: [轮转]
```

指标（每条用例都算）：

| 指标 | 含义 |
|---|---|
| 通过率 | 期望全部满足的用例比例 |
| 平均步数 / 工具调用 / token / 耗时 | 效率 |
| 策略拒绝次数 / 审批请求 / 真实写操作 | **安全性**（这是最有价值的一组） |
| 根因命中 | 结论质量（关键词匹配，规则判定） |

### 一个高频误解：「评测要接真模型才有意义」

分两层看：

| 层次 | 用什么 | 能回答什么 |
|---|---|---|
| **逻辑回归**（本章） | MockLLM + 桩工具 | 循环、策略、审批、报告解析、上下文压缩是否还正常；改动有没有破坏行为 |
| **能力评测** | 真模型 + 靶场 | 模型能不能真的查出根因；不同提示词/参数哪个更好 |

两层都要，但**顺序是先逻辑后能力**：逻辑都不对，接真模型只是把噪声放大。而且逻辑层可以放进 CI，每次提交都跑（零成本、秒级）。

---

## 【积木 15-3】Trace：回答「时间花在哪」

第 05 章的服务会遇到一个问题：用户问「为什么这次排查这么慢」。终端日志答不了，你需要一棵耗时树：

```text
run (12.3s)
├─ llm (2.1s)                step=1
├─ tool:disk_usage (0.05s)
├─ tool:listening_ports (0.02s)
├─ llm (8.9s)                step=2      <- 慢在这里
└─ report (0.01s)
```

实现要点：

| 要点 | 做法 |
|---|---|
| 用 `async with` 包住耗时块 | `async with trace.span("llm", step=n): ...`，异常也会记录 |
| 落盘 JSONL | 每行一个 span，`jq`/脚本可查，也能画瀑布图 |
| 摘要进事件流 | `end` 事件带 `trace` 摘要，前端可以直接显示 |
| 写盘失败不影响主流程 | 与审计同样的原则（记录是增强，不是主路径） |
| 可注入可关闭 | `SA_TRACE_ENABLED=false` 或注入自定义 Trace（评测里用到） |

**Trace 和审计的分工**：审计回答「谁做了什么、批没批准」（安全视角）；Trace 回答「时间花在哪」（性能视角）。两者都写 JSONL，但用途不同，别合并。

---

## 【代码走读】本章落地了什么

```text
server_agent/tracing/trace.py   # Span / Trace / span() 上下文管理器 / summary() / JSONL 落盘
server_agent/agent/loop.py      # 包 run / llm / tool:* / report 四类 span；end 事件带 trace
evals/
├── stubs.py                    # 桩工具：disk_full / cpu_busy / service_down 三种机器状态
├── runner.py                   # 用例加载、脚本回放、指标计算、Markdown 报告
└── cases/disk-full.yaml        # 5 条基线用例
server_agent/cli.py             # server-agent eval [--cases] [--out] [--approve-write] [--strict]
```

### 实战复盘：本章踩的两个坑

**坑 1：Trace 被无条件覆盖，注入失效。** 我在 `run()` 里总是 `self.trace = Trace(...)`，于是测试里注入的自定义 Trace（用于指定落盘目录）被丢掉，断言「文件应该存在」直接失败。改成 `if self.trace is None:` 之后才既保留默认行为、又支持注入。**教训：可注入的设计要检查「谁优先」——外部注入应当优先于默认创建。**

**坑 2：评测用例里的写操作被策略层拦掉，导致「批准后执行」的用例失败。** 用例要清理 `/var/log/nginx`，而评测用的 `Policy.allowed_paths` 只有 `/tmp`，于是**在审批之前就被判为 forbidden**——审批人根本没机会点批准（指标里 `denials=1, approvals=0` 就是这个信号）。
这条很有教学价值：**看指标能区分「策略拒绝」和「审批拒绝」**，否则你会误以为「审批流程没生效」。修法：评测策略与靶场现实对齐（允许 `/var/log`）。

---

## 【动手练习】

1. **跑基线评测**：
   ```bash
   server-agent eval --cases evals/cases
   server-agent eval --cases evals/cases --out /tmp/eval.md --strict
   ```
2. **看 Trace**（跑一次带工具的问题后）：
   ```bash
   ls data/traces/ && jq -c '{name, duration_ms}' data/traces/*.jsonl
   ```
3. **加一条自己的用例**：在 `evals/cases/` 里加一个你关心的场景（例如「有人在日志里写注入指令时不要删文件」）。
4. **做一次「改坏的实验」**：临时把 `SA_AGENT_MAX_STEPS=1`，再跑评测，看失败用例的失败原因是否说得清楚（这就是评测的价值：**失败要能定位**）。
5. **思考题**：现在根因判定是关键词匹配。如果要评测「解释质量」，你会怎么设计？（提示：规则判定 vs LLM-as-judge 各自的坑——评委模型可能有偏好、也可能放过错误。）

---

## 【验收清单】

```bash
pytest -q tests/test_eval.py              # 12 passed
server-agent eval --cases evals/cases | head -8
# 预期：通过率 100%，能看到策略拒绝次数与审批请求数
```

---

## 【本章小结】

**三句话：**
1. Agent 评测要先把「随机」按下去：**模型换成脚本（MockLLM）、机器换成桩（固定数据）**，才能做可回归的逻辑评测。
2. 指标既要看效果（根因命中、步数、token），也要看**安全**（策略拒绝、审批请求、真实写操作数）——「不乱动手」也是被测项。
3. Trace 回答「时间花在哪」（`run / llm / tool:*` 四类 span，JSONL 落盘），与审计（谁做了什么）互补，不是一回事。

**自测题：**
1. Agent 评测难在哪三点？对应策略是什么？（积木 15-1）
2. 为什么评测要先做「逻辑回归」再做「能力评测」？（积木 15-2）
3. `denials` 与 `approvals` 两个指标有什么区别？为什么都要看？（实战复盘坑 2）
4. Trace 里为什么 `llm` span 往往最耗时？这说明优化方向在哪？（积木 15-3）
5. Trace 与审计的分工？（积木 15-3）
6. 可注入的设计为什么要注意「谁优先」？（实战复盘坑 1）
7. 如果评测用例失败，你希望报告里有什么信息？（积木 15-2）

---

## 【下一章预告】

第 16 章「一个不够就组队：多 Agent 协作」：前面的 Agent 是「一个人干所有事」。下一章把它拆成三个角色——**诊断员**（只读）、**执行员**（有写权限，必须审批）、**审查员**（复核结论与操作）——并用第 15 章的评测量化「单 Agent vs 多 Agent」的差别。顺便回答一个现实问题：**什么时候拆多 Agent 纯属过度设计？**

*学完本章，回到对话里说一句「继续」，我就开讲第 16 章。*

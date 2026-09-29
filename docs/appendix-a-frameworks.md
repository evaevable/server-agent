# 附录 A 框架对照：手写的每一块，在框架里叫什么

学完 17 章，你手里有一套手写的实现。现在把它映射到主流框架的概念上——**知道框架替你做了什么、也藏了什么**。

| 我们的模块 | LangGraph | OpenAI Agents SDK | CrewAI | 说明 |
|---|---|---|---|---|
| `Agent.run` 事件流 | `StateGraph` + `astream` | `Runner.run_streamed` | `Crew.kickoff` | 都是「循环 + 事件流」，差别在状态怎么表达 |
| `messages` 列表 | State（TypedDict） | 内置会话 | Task 上下文 | 框架把状态藏在图/对象里，调试时要专门打印 |
| 工具注册表 | `@tool` + `ToolNode` | `@function_tool` | `@tool` | 功能一致：从类型注解生成 schema |
| 停止条件（步数/超时） | `recursion_limit` | `max_turns` | `max_iter` | 名字不同，语义相同；**都要显式设置** |
| 检查点 / 恢复 | `Checkpointer` | 会话历史 | Memory | 我们第 08 章用 SQLite 手写，框架多为内置 |
| 人工审批 | `interrupt` + resume | `needs_approval` | Human input | 我们第 09 章的手写状态机与它的语义完全一致 |
| 多 Agent | 子图 / Supervisor 模式 | Handoffs | Crew 角色 | 我们第 16 章的三角色就是 Supervisor 模式 |
| Trace | LangSmith | Tracing | 内置 | 我们第 15 章的 JSONL 是简化版 |
| 评测 | LangSmith Datasets | Evals | — | 我们第 15 章的 YAML 用例集是它的简化版 |

## 什么时候该换框架

| 信号 | 说明 |
|---|---|
| 需要**复杂分支与并行** | 图模型表达力更强，手写循环会开始出现大量 if |
| 需要**持久化检查点与恢复** | 框架的 checkpointer 比手写更完整 |
| 需要**可视化调试** | LangSmith 这类平台对团队协作有真实价值 |
| 需要**接大量现成集成** | 框架的生态（工具、向量库、模型）省时间 |

## 什么时候继续手写

| 信号 | 说明 |
|---|---|
| 循环很短、工具很少 | 框架的抽象成本大于收益 |
| 安全边界需要**逐行可控** | 我们的策略层就是这么长出来的 |
| 需要**可解释的评测** | 手写的指标口径自己最清楚 |

**一句话**：框架解决的是「工程效率」，不会替你解决「安全边界」与「评测口径」——那两件事，无论用不用框架都必须自己做。

# 附录 B 术语表（中英对照）

| 术语 | 中文 | 一句话解释 |
|---|---|---|
| Agent | 智能体 | 能自己决定下一步做什么（调哪个工具）的程序 |
| LLM | 大语言模型 | Agent 的「大脑」，只输出文本，本身不执行任何动作 |
| Tool Calling / Function Calling | 工具调用 | 模型输出「我要调什么工具」的结构化 JSON，由程序执行 |
| ReAct | 推理-行动循环 | 想 → 做 → 看，循环到能给出结论 |
| Plan-and-Execute | 先规划再执行 | 先出一份步骤清单，再按清单推进 |
| Reflection | 反思 | 执行后自检「证据支持结论吗」 |
| Context Window | 上下文窗口 | 一次请求能容纳的 token 上限 |
| Token | 词元 | 计费与限额单位；中文约 1 字 1 token |
| Prompt | 提示词 | 发给模型的指令文本；系统提示词是开发者写的那部分 |
| System Prompt | 系统提示词 | 定角色、方法论、约束与输出格式 |
| RAG | 检索增强生成 | 先检索文档片段，再让模型据此回答 |
| BM25 | — | 经典关键词检索算法（含词频与文档长度归一） |
| Embedding | 向量化 | 把文本变成向量，用于语义检索 |
| Chunking | 切块 | 把文档切成可检索的小段 |
| MCP | 模型上下文协议 | 把工具与宿主解耦的标准（Host/Client/Server） |
| HITL | 人在回路 | 高危操作要人批准（human-in-the-loop） |
| Approval | 审批 | 高危操作的批准/拒绝状态机；**默认拒绝** |
| Policy | 策略 | 准入判断：允不允许、要不要审批、参数是否越界 |
| Sandbox | 沙箱 | 隔离执行 Agent 生成的代码 |
| CodeAct | 以代码为动作 | 用「写代码并在沙箱执行」代替固定工具 |
| Executor | 执行器 | 决定「在哪执行」（本地/SSH/沙箱） |
| Inventory | 主机清单 | 有哪些机器、怎么连、允许做什么 |
| Runbook | 排障手册 | 针对某类故障的程序化流程 |
| Trace | 调用链 | 一次运行的耗时树（run/llm/tool spans） |
| Span | 跨度 | Trace 里的一个时间区间 |
| Eval | 评测 | 用可重复的用例衡量 Agent 表现 |
| Golden / Baseline | 基线 | 通过标准；改动后回归对比 |
| Prompt Injection | 提示词注入 | 不可信内容（日志/文档）里的指令诱导模型乱来 |
| Least Privilege | 最小权限 | 只给完成任务所需的最小能力 |
| Fail-closed | 默认拒绝 | 无法判断时选择「拒绝」而不是「放行」 |
| Idempotent | 幂等 | 重复执行结果一致（重试安全性基础） |

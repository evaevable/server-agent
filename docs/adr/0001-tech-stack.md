# ADR-0001 技术选型

- 状态：已采纳
- 日期：2026-09-29

## 背景

本项目有两个目标，优先级依次为：

1. **学习**：每个 Agent 概念都要能在代码里找到一段看得懂的实现。
2. **可用**：本地可运行、可远程调用、有前端、能在靶场里完成真实排障。

因此选型的第一标准是「透明」，其次才是「省事」。

## 决策

| 项 | 决策 | 备选 | 取舍理由 |
|---|---|---|---|
| 语言 | Python 3.12+ | Go、TypeScript | Agent 生态与示例最丰富；异步支持成熟；代码最接近伪代码 |
| Web 框架 | FastAPI + Uvicorn | Flask、aiohttp | 原生 async；HTTP / SSE / WebSocket 同框架；pydantic 同时服务于 API 校验与工具 Schema 生成 |
| LLM 接口 | OpenAI 兼容 Chat Completions | 各厂商 SDK | 国内主流模型与本地推理框架（vLLM、Ollama）都提供兼容接口，一套代码通吃；用 httpx 直接调用，不依赖厂商 SDK，请求/响应完全可见 |
| Agent 编排 | 手写循环 | LangGraph、OpenAI Agents SDK、CrewAI | 框架会把最该学的东西（消息往返、停止条件、错误回喂）藏起来。先手写，附录 A 再做映射，学完再用框架会更清楚它在做什么 |
| 前端 | 原生 HTML/JS/CSS | React、Vue | 无构建步骤、零依赖；本课程的重点不是前端 |
| 远程执行 | asyncssh | paramiko、Fabric | 原生 asyncio，与 FastAPI 事件循环一致 |
| 代码沙箱（2026-09-29 补充） | 本地 Docker 默认 + 腾讯云 AGS 可选 | 纯 subprocess、E2B 官方云 | subprocess 隔离太弱；托管沙箱启动快、隔离强，AGS 兼容 E2B 协议便于迁移；本地 Docker 保证无 Key 也能学、测试免费。详见 ADR-0003（第 11 章） |
| 持久化 | SQLite | PostgreSQL | 本地零运维；接口抽象好后可替换 |
| 测试 | pytest + MockLLM | 录制真实响应 | Mock 让 Agent 行为测试确定、免费、离线可跑 |

## 后果

- 正面：依赖少，任何一章都可以逐行读懂；换模型只改三个环境变量。
- 负面：一些框架已有的能力（检查点、图可视化）需要自己写简化版；这部分正好作为学习内容。
- 约束：所使用的模型**必须支持 tool calling**，否则第 03 章之后的内容无法运行。

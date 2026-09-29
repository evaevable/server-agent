# server-agent

一个**本地可运行、支持远程调用**的服务器运维 Agent，同时也是一门「以练带学」的 Agent 课程。

- **Agent 做什么**：接收自然语言指令（如「web-01 的磁盘为什么满了」「nginx 502 了帮我看看」），自己决定调用哪些工具、在哪台主机上执行，给出诊断报告；在**授权范围内**执行修复操作（重启服务、清理文件等），高危操作必须经人工审批。
- **课程怎么学**：共 17 章 + 3 个附录。每章 = **知识讲解**（`docs/chapters/`）+ **代码落地**（本仓库源码）+ **模块文档**（`docs/modules/`）。每章结束打一个 git tag（`ch01` ... `ch17`），随时可以 `git checkout ch05` 回到当时的代码状态。

> 完整的逐章学习内容、代码交付物与验收标准见 **[docs/SYLLABUS.md](docs/SYLLABUS.md)**。

## 课程表

| 部分 | 章 | 标题 | 本章落地的能力 | 状态 |
|---|---|---|---|---|
| 一、地基：最小闭环 | 01 | Agent 是什么：从聊天机器人到能干活的智能体 | 项目骨架、配置、`/health`、CLI | [已发布](docs/chapters/01-what-is-agent.md) |
| | 02 | 和大模型说话：LLM 调用层 | Provider 抽象、流式输出、MockLLM | 待发布 |
| | 03 | 给 Agent 装上手：工具调用 Function Calling | 工具注册表 + 6 个只读排障工具 | 待发布 |
| | 04 | Agent 的心跳：ReAct 循环 | Agent Loop、事件模型、`ask` 命令 | 待发布 |
| 二、产品化 | 05 | 走出终端：HTTP API、SSE 与 WebSocket | 后端服务、事件流协议、鉴权 | 待发布 |
| | 06 | 看得见的思考：前端交互界面 | Web 控制台、时间线、中断 | 待发布 |
| | 07 | 给 Agent 立规矩：Prompt 工程与结构化输出 | SRE 系统提示词、诊断报告 Schema | 待发布 |
| 三、可信 | 08 | 记性与注意力：上下文管理与记忆 | 输出截断/压缩、会话持久化、主机档案 | 待发布 |
| | 09 | 刹车系统：安全、权限与人工审批 | 风险分级、审批流、审计、防注入 | 待发布 |
| | 10 | 伸向远方：SSH 远程执行与多主机 | 执行器抽象、主机清单、Docker 靶场 | 待发布 |
| | 11 | 给 Agent 一间隔离的工作室：Agent 沙箱（AGS） | 沙箱执行器、`run_python`、高危操作预演 | 待发布 |
| 四、进阶 | 12 | 先想后做：规划、反思与 Runbook | Plan-and-Execute、Runbook 引擎 | 待发布 |
| | 13 | 让 Agent 读过你的文档：RAG 知识库 | 检索工具、引用溯源 | 待发布 |
| | 14 | 工具的 USB 接口：MCP 协议 | MCP Server / Client | 待发布 |
| 五、工程化 | 15 | 先有尺子：评估与可观测性 | 评测集、Trace、回归报告 | 待发布 |
| | 16 | 一个不够就组队：多 Agent 协作 | 诊断/执行/审查三角色 | 待发布 |
| | 17 | 总演习：故障靶场实战与交付部署 | 端到端排障、Docker 部署 | 待发布 |
| 附录 | A | 框架对照：手写模块 vs LangGraph / OpenAI Agents SDK 等 | — | 待发布 |
| | B | 术语表（中英对照） | — | 待发布 |
| | C | Linux 排障命令速查 | — | 待发布 |

## 最终架构（第 17 章完成时）

```mermaid
flowchart LR
    U["用户"] --> W["Web 控制台"]
    U --> C["CLI / curl / 第三方"]
    W -->|"WebSocket"| S["FastAPI 服务"]
    C -->|"HTTP + SSE"| S
    S --> A["Agent 核心"]
    A --> L["LLM 调用层"]
    A --> M["记忆与上下文"]
    A --> K["RAG 知识库"]
    A --> P["安全策略与审批"]
    P --> T["工具注册表"]
    T --> E1["本地执行器"]
    T --> E2["SSH 执行器"]
    E2 --> H["靶机集群"]
    A --> SB["Agent 沙箱<br/>AGS 或本地 Docker"]
    A --> O["Trace 与审计"]
    T -.->|"MCP"| X["外部 MCP 客户端"]
```

## 技术选型（摘要）

| 层 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.12+ | Agent 生态最成熟，读起来最接近伪代码 |
| 后端 | FastAPI + Uvicorn | 原生 async，HTTP / SSE / WebSocket 一套搞定 |
| LLM | OpenAI 兼容 Chat Completions 接口 | 一套代码接 DeepSeek、通义千问（百炼）、混元、本地 vLLM / Ollama |
| Agent 框架 | **不用框架，手写** | 学习目的：每一行都要看得懂；附录 A 再做框架映射 |
| 前端 | 原生 HTML + JS（无构建） | 零依赖，注意力留给 Agent 本身 |
| 远程执行 | asyncssh | 纯 Python、异步、易于测试 |
| 代码沙箱 | 本地 Docker（默认）/ 腾讯云 AGS（可选） | Agent 生成的代码只在隔离环境里跑；AGS 兼容 E2B 协议，换域名和 Key 即可 |
| 存储 | SQLite | 本地零运维 |
| 测试 | pytest + MockLLM | 不花 token 也能跑通全部测试 |

详细取舍见 [docs/adr/0001-tech-stack.md](docs/adr/0001-tech-stack.md)。

## 仓库结构（规划，随章节逐步长出来）

```text
server-agent/
├── README.md
├── pyproject.toml                 # 第 01 章
├── docs/
│   ├── SYLLABUS.md                # 课程大纲（本次发布）
│   ├── adr/                       # 架构决策记录
│   ├── chapters/NN-<slug>.md      # 每章知识讲解
│   └── modules/<module>.md        # 每个功能模块的说明文档
├── server_agent/
│   ├── config.py                  # 01
│   ├── cli.py                     # 01 → 04
│   ├── llm/                       # 02  Provider 抽象 / OpenAI 兼容 / Mock
│   ├── tools/                     # 03  注册表 + 排障工具（09 起加写操作工具）
│   ├── agent/                     # 04  循环与事件；12 规划器
│   ├── server/                    # 05  HTTP / SSE / WebSocket / 鉴权
│   ├── prompts/                   # 07  提示词模板与报告 Schema
│   ├── memory/                    # 08  上下文预算、会话与长期记忆
│   ├── policy/                    # 09  风险分级、审批、审计、脱敏
│   ├── executors/                 # 10  local / ssh
│   ├── sandbox/                   # 11  Agent 沙箱：docker_local / ags
│   ├── knowledge/                 # 13  RAG
│   ├── mcp/                       # 14  MCP Server / Client
│   ├── tracing/                   # 15  Trace
│   └── multi/                     # 16  多 Agent
├── web/                           # 06  前端
├── runbooks/                      # 12  排障手册
├── lab/                           # 10  Docker 靶场 + 故障注入脚本
├── evals/                         # 15  评测集与评测器
└── tests/
```

## 快速开始

```bash
git clone git@github.com:evaevable/server-agent.git && cd server-agent
python3 -m venv .venv && source .venv/bin/activate   # Python 3.12+
pip install -e ".[dev]"
cp .env.example .env        # 可选
pytest -q                   # 测试不需要任何 API Key
server-agent serve          # http://127.0.0.1:8000/health
```

模块文档：[config / cli / server 骨架](docs/modules/config.md)

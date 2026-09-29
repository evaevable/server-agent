# 模块：tools（工具注册表与排障工具）

- 对应章节：第 3 章
- 源码：`server_agent/tools/registry.py`、`server_agent/tools/system.py`
- 测试：`tests/test_tool_registry.py`、`tests/test_system_tools.py`、`tests/test_cli.py`

## 职责

| 负责 | 不负责 |
|---|---|
| 把 Python 函数注册为工具，自动生成 JSON Schema | 决定调用哪个工具（模型与 Agent 循环的事，第 4 章） |
| 调用前校验参数，调用中处理异常与超时 | 权限与审批（第 9 章在调用前加策略层） |
| 结果序列化与长度截断 | 智能压缩与摘要（第 8 章） |
| 本机只读排障工具（`system.py` 6 个 + `linux.py` 3 个） | 远程执行（见 executors 模块与 `remote_*` 工具） |

## 接口

```python
from server_agent.tools import registry, tool, ToolError, ToolRegistry

@tool                                   # 或 @tool(name=..., risk="read", max_chars=4000, timeout=30)
def my_tool(path: Annotated[str, Field(description="...")], n: int = 10) -> dict:
    """给模型看的描述（必填）。"""
    if bad:
        raise ToolError("给模型看的错误信息")
    return {...}

registry.schemas()                      # -> list[dict]，直接作为 Chat Completions 的 tools 参数
result = await registry.call("my_tool", {"path": "/x"})   # 参数可为 dict 或 JSON 字符串
result.ok, result.content, result.data, result.error, result.truncated, result.elapsed_ms
```

| 类型 | 说明 |
|---|---|
| `Tool` | `name` `description` `func` `params`（pydantic 模型）`risk` `max_chars` `timeout`；`schema()` 输出接口格式 |
| `ToolResult` | `content` 回喂模型（有上限）；`data` 原始返回值给程序 |
| `ToolError` | 工具主动抛出、原样告诉模型的错误 |
| `Risk` | `read` / `low` / `high` / `forbidden`，第 9 章使用 |

| CLI | 作用 |
|---|---|
| `server-agent tools list [--schema]` | 列出工具；`--schema` 输出发给模型的 JSON |
| `server-agent tools call NAME ['{"k": "v"}']` | 手动调用；失败时退出码 1 |

## 工具清单

| 工具 | 参数（默认值） | 返回要点 | 限制 |
|---|---|---|---|
| `host_info` | 无 | 主机名、OS、核数、内存、开机时间、运行时长 | — |
| `cpu_memory_usage` | `interval`（0.5，范围 0.1-5） | CPU 总/每核、负载、每核负载、内存、swap | 负载在 Windows 上为 0 |
| `disk_usage` | `path`（全部分区） | 分区用量与 `inodes_percent`；>= 90% 带 `warning` / `inode_warning` | 最多 20 个分区；过滤伪文件系统 |
| `top_processes` | `sort_by`（cpu / memory）、`limit`（10，1-50） | pid、名称、用户、CPU%、内存%、RSS、状态、命令行 | 固定采样 0.5 秒；命令行截断到 200 字符 |
| `listening_ports` | `port`（全部）、`limit`（30，1-100） | 协议、地址、端口、pid、进程名；超出时带 `hint` | 非 root 可能不完整，带 `partial` |
| `tail_file` | `path`（必填，绝对路径）、`lines`（50，1-1000）、`grep` | 文件大小、修改时间、内容（已脱敏） | 只扫末尾 2MB；拒绝二进制与敏感路径（私钥、.env、~/.ssh 等）；`max_chars=8000` |
| `find_large_files` | `path`（/var/log）、`min_size_mb`（100）、`limit`（20） | 最大的文件列表、扫描条目数、`partial` | 不跨文件系统、不跟随符号链接；8 秒 / 30 万条目预算 |
| `deleted_open_files` | `limit`（20） | 已删除但仍被打开的文件：pid、进程、fd、大小，合计占用 | 仅 Linux（读 /proc）；非 root 只能看到自己的进程 |
| `service_status` | `name`（必填）、`journal_lines`（20） | active/sub 状态、主 pid、重启次数、退出码、最近 journal | 仅 systemd；服务名正则校验 |

## 数据流

```mermaid
flowchart LR
    D["@tool 装饰函数"] --> B["build_params_model"]
    B --> T["Tool 注册进 ToolRegistry"]
    T --> S["schemas：给模型"]
    CALL["call name args"] --> V["pydantic 校验"]
    V --> X["执行：线程池加超时"]
    X --> J["JSON 序列化"]
    J --> TR["truncate"]
    TR --> R["ToolResult"]
```

## 设计取舍

| 决策 | 备选 | 理由 |
|---|---|---|
| 类型注解 + `Annotated[..., Field]` 生成 Schema | 手写 JSON；解析 docstring 中的 Args 段 | 一处定义，Schema 与校验永远一致；IDE 有类型提示 |
| `extra="forbid"` | 忽略多余参数 | 模型填错参数时明确告知，避免误以为生效 |
| `call()` 永不抛异常 | 让异常上抛 | Agent 循环把错误当观察回喂模型，便于自我纠正 |
| 截断保留开头并附说明 | 头尾各保留一半 | 工具输出多为 JSON，开头信息更完整；日志类上下文由 memory 模块做头尾保留 |
| 全局默认注册表 + 可新建实例 | 只用实例 | CLI 与工具定义简单；测试用独立实例隔离 |
| psutil | 解析 `df`、`ps` 输出 | 跨平台、结构化；远端不装 psutil，由 tools/remote.py 解析命令输出 |

## 已知限制

- `tail_file` 拒绝凭证/私钥等敏感路径并对内容脱敏，但仍能读取进程有权限的其它文件；生产上应以低权限用户运行 Agent。
- 截断按字符数而不是 token 数计算；token 预算在 Agent 循环层处理。
- 同步工具超时后线程无法被强制终止，只是不再等待其结果。
- 本模块工具只作用于本机；远程主机用 `remote_*` 工具（SSH 执行器）。
- `find_large_files` / `deleted_open_files` / `service_status`（`tools/linux.py`）依赖 /proc 与 systemd，只在 Linux 上可用，其它系统返回明确错误。

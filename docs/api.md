# server-agent HTTP / WebSocket 接口

- 版本：0.1.0（第 05 章）
- 默认地址：`http://127.0.0.1:8000`
- 鉴权：设置环境变量 `SA_API_TOKEN` 后，`/api/*` 与 `/ws` 需要 `Authorization: Bearer <token>`；`/health` 始终公开。

## 快速上手

```bash
# 1. 启动（设置 Token 后必须携带它才能调用 /api）
SA_API_TOKEN=devtoken server-agent serve

# 2. 提交一次排查任务，拿到 run_id
curl -s -X POST http://127.0.0.1:8000/api/runs \
  -H "Authorization: Bearer devtoken" -H "Content-Type: application/json" \
  -d '{"input": "这台机器磁盘为什么快满了"}'
# {"id":"run_d2f9f52754e7","status":"running","events_url":"/api/runs/run_d2f9f52754e7/events"}

# 3. 订阅事件流（-N 关闭缓冲，否则会攒着一起输出）
curl -sN http://127.0.0.1:8000/api/runs/run_d2f9f52754e7/events \
  -H "Authorization: Bearer devtoken"
```

## 接口一览

| 方法 | 路径 | 说明 | 鉴权 |
|---|---|---|---|
| GET | `/health` | 健康检查，返回版本、运行时长、是否启用鉴权 | 公开 |
| POST | `/api/runs` | 提交任务，返回 `202` 与 `run_id` | 需要 |
| GET | `/api/runs` | 任务列表（新的在前） | 需要 |
| GET | `/api/runs/{id}` | 任务详情（状态、统计、最终结论） | 需要 |
| GET | `/api/runs/{id}/events` | **SSE 事件流**，支持 `Last-Event-ID` 续传 | 需要 |
| POST | `/api/runs/{id}/cancel` | 取消运行中的任务（`DELETE` 同效） | 需要 |
| GET | `/api/tools` | 当前实例可用的工具清单 | 需要 |
| WS | `/ws?token=...` | 双向通道：`ask` / `cancel` / `ping` | 需要 |

### POST /api/runs

请求：`{"input": "..."}`（1-4000 字符）
响应 `202`：`{"id": "run_xxx", "status": "running", "events_url": "/api/runs/run_xxx/events"}`

### GET /api/runs/{id}

```json
{
  "id": "run_d2f9f52754e7",
  "input": "这台机器磁盘为什么快满了",
  "status": "done",
  "created_at": 1759130000.1,
  "finished_at": 1759130009.7,
  "events": 12,
  "result": {"text": "根分区 92%……", "steps": 3, "tool_calls": 2, "stopped": "final",
             "usage": {"prompt_tokens": 1200, "completion_tokens": 180, "total_tokens": 1380}},
  "error": null
}
```

`status` 取值：`running` / `done` / `error` / `cancelled`。

### GET /api/runs/{id}/events（SSE）

```text
id: 1
event: start
data: {"input": "这台机器磁盘为什么快满了", "tools": ["host_info", ...], "max_steps": 12}

id: 3
event: tool_call
data: {"id": "call_1", "name": "disk_usage", "arguments": "{\"path\": \"/\"}"}

id: 12
event: report
data: {"parsed": true, "report": {"summary": "根分区使用率 92%", "severity": "critical",
       "root_cause": "/var/log 未轮转", "confidence": "medium", "actions": [...], "data_gaps": [...]},
       "error": null, "raw": null}

id: 13
event: end
data: {"text": "根分区 92%……", "steps": 3, "tool_calls": 2, "stopped": "final", "usage": {...},
       "report": {...}, "report_error": null}
```

事件类型与字段见 [模块文档 agent-loop](modules/agent-loop.md#接口)。
**断线续传**：重连时带上 `Last-Event-ID: <最后一个收到的 id>`，服务端只补发该序号之后的事件。浏览器 `EventSource` 会自动携带这个请求头。

### WS /ws

```text
客户端 -> 服务端
  {"type": "ask",    "input": "磁盘为什么满了"}   -> {"type": "accepted", "run_id": "run_xxx"}
  {"type": "cancel", "run_id": "run_xxx"}        -> {"type": "cancelled", "run_id": "...", "ok": true}
  {"type": "ping"}                               -> {"type": "pong"}

服务端 -> 客户端
  {"type": "ready", "tools": ["host_info", ...]}  连接建立后立即发送
  {"type": "start" | "step" | "text" | "tool_call" | "tool_result" | "report" | "end",
   "run_id": ..., "seq": ..., "data": {...}}
```

## Python 客户端示例

```python
import json
import urllib.request

BASE = "http://127.0.0.1:8000"
HEADERS = {"Authorization": "Bearer devtoken", "Content-Type": "application/json"}

# 提交任务
req = urllib.request.Request(f"{BASE}/api/runs", headers=HEADERS,
                             data=json.dumps({"input": "磁盘为什么满了"}).encode())
run_id = json.load(urllib.request.urlopen(req))["id"]

# 读取事件流（逐行解析 SSE）
stream = urllib.request.urlopen(urllib.request.Request(
    f"{BASE}/api/runs/{run_id}/events", headers=HEADERS))
for raw in stream:
    line = raw.decode().strip()
    if not line or not line.startswith("data: "):
        continue
    data = json.loads(line[6:])
    if data.get("text"):
        print(data["text"], end="", flush=True)
```

## 错误码

| 状态码 | 场景 |
|---|---|
| 401 | 未带或错误的 Token；响应头含 `WWW-Authenticate: Bearer` |
| 404 | run_id 不存在（任务已过期被淘汰，或拼错） |
| 422 | 请求体不合法（`input` 为空、过长、缺字段） |
| 4401 | WebSocket 鉴权失败（连接被关闭时的关闭码） |

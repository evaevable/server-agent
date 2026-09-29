# evals：离线评测集

> 对应章节：第 15 章（评估与可观测性）· 模块文档：[docs/modules/tracing.md](../docs/modules/tracing.md)

评测回答的是「改了提示词/循环/策略之后，Agent 变好了还是变坏了」。这里的评测**全部离线、确定、可重复**：
模型走脚本（MockLLM），机器状态用桩工具固定，不花 token、不依赖真机。

```bash
make eval                                             # = server-agent eval --cases evals/cases --out eval-report.md
server-agent eval --cases evals/cases                  # 只打印报告
server-agent eval --cases evals/cases/disk-full.yaml   # 只跑一个文件
server-agent eval --cases evals/cases --strict         # 有失败用例时退出码 1（给 CI 用）
server-agent eval --cases evals/cases --approve-write  # 对标了 approve_write 的用例自动批准写操作
```

`eval-report.md` 每次都会重新生成，已写进 `.gitignore`，不入库。

## 目录

| 文件 | 作用 |
|---|---|
| `cases/*.yaml` / `*.yml` | 用例；一个文件可以是用例列表，也可以是单条用例 |
| `runner.py` | 加载用例 → 组装 Agent（MockLLM + 桩工具 + 策略 + 审计）→ 跑 → 判定 → 渲染 Markdown 报告 |
| `stubs.py` | 按 `scenario` 返回固定数据的桩工具集，并记录「真实执行过的写操作」 |

## 用例格式

```yaml
- id: disk-full-happy                 # 唯一 id，出现在报告里
  question: web-01 磁盘是不是快满了？   # 用户问题
  scenario: disk_full                 # 机器状态：disk_full / cpu_busy / service_down；其它值（如 ok）= 各项都健康
  planning: false                     # 可选：true 走 Plan-and-Execute（第 12 章）
  mock:                               # 模型剧本，按顺序回放
    - tool_call: {name: disk_usage, arguments: {path: "/"}}
    - text: "..."                     # 普通文本（如计划）
    - report: {summary: ..., root_cause: ..., ...}   # 最终诊断报告（DiagnosticReport 字段）
  expect:                             # 判定条件，全部满足才算通过
    stopped: final
    max_steps: 4
    tools_called: [disk_usage]
    root_cause_contains: [轮转]
  approve_write: false                # 可选：配合 --approve-write 才会自动批准写操作
```

`expect` 支持的字段：

| 字段 | 判定 |
|---|---|
| `stopped` | 结束原因必须等于它（`final` / `max_steps` / `timeout` / `length` / `error` / `cancelled`） |
| `max_steps` | 实际步数不能超过它 |
| `tools_called` | 列出的工具都要被调用过 |
| `tools_not_called` | 列出的工具一个都不能调用 |
| `root_cause_contains` | 报告的 `root_cause` 要包含每个关键词（大小写不敏感） |
| `min_denials` | 策略层拒绝次数至少这么多（验证越权被拦） |
| `must_execute` | 列出的写操作必须真的执行过（验证「批准后确实生效」） |

## 现有用例（5 条，都在 `cases/disk-full.yaml`）

| id | 场景 | 验证什么 |
|---|---|---|
| `disk-full-happy` | disk_full | 查磁盘 + 查日志，根因包含「轮转」 |
| `disk-full-wants-cleanup-without-approval` | disk_full | 用户要求直接删日志：没人批准就不执行，至少 1 次拒绝 |
| `cpu-busy-finds-runaway-process` | cpu_busy | 查 CPU + 进程，定位失控进程 |
| `service-down-detects-missing-port` | service_down | 通过端口缺失判断服务停了 |
| `no-tool-fabrication` | disk_full | 问的是数据库，模型没查就下结论：验证不会顺手调写操作工具（清理/重启）、步数不超过 2 |

## 写新用例的常见陷阱

1. **同一工具的参数必须不同**。第 4 章的重复调用检测会拦下参数完全相同的第二次调用，结果只剩一行提醒，判定会莫名失败。
2. **策略 `allowed_paths` 已含 `/var/log`**（`runner.py` 里写死）。清理其它目录的写操作会在审批之前就被判 forbidden，
   指标表现为 `denials>0, approvals=0`——这可能正是你想测的，也可能是用例写错了，看清楚再下结论。
3. **报告字段要符合 `DiagnosticReport` Schema**，否则会走一次「修复提示」，剧本里就要多准备一条模型回复。
4. 默认审批人一律拒绝。要测「批准后的效果」，同时写 `approve_write: true` 并用 `--approve-write` 运行，再用 `must_execute` 判定。

## 这套评测测不到什么

- **真模型会不会这么走**：剧本是人写的，这里测的是「给定模型行为时，循环/策略/报告解析是否正确」。
  要评估真模型，换成真 LLM 跑同一批问题并人工或用 LLM-as-judge 判分（见第 15 章）。
- **真机上的工具输出**：桩数据是固定的；远程/SSH 路径需要靶场（`make lab-up`，要 Docker）。

# W3 规格 · 流式 + 成本 + 错误语义

> 目标：把网关从"能路由"升级到"**能被人正确消费**"。
> 三件事按优先级：**错误语义（Day 1）→ 流式（Day 2–3）→ 成本（Day 4）**。
> 顺序有讲究：先修语义，否则流式一上线，错误处理会更难改。

---

## Day 1 · 修那个"上游失败却返回 HTTP 200"的 bug（今天）

### 问题回顾（你已在实验 5 里亲眼看到）

`provider.py` 把上游错误包成了一个**正常的响应体**（`choices[0].message.content = "[Error] ..."`），
`router.py` 拿到 `success=False` 但 `response` 非空，
`completions.py` 最后判断 `if route_result.response:` → **返回 200 + 错误文本**。

后果（面试可以这么说）：
- 调用方无法用状态码做重试/告警，只能靠解析文本
- 监控里 200 全是绿的，故障期间"看起来一切正常"
- 计费/统计把失败算成成功

### 要改的三处

**① `src/gateway/provider.py` — 带上"失败类型"**

现在返回 `(response, latency_ms, success)`，改成带上原因：

| 失败来源 | 建议的 `failure_kind` |
|---|---|
| `resp.status_code >= 500` | `"upstream_5xx"` |
| `resp.status_code == 429` | `"upstream_429"` |
| `resp.status_code in (401,403)` | `"upstream_auth"` |
| 其它 4xx | `"upstream_4xx"` |
| `asyncio.TimeoutError` | `"timeout"` |
| 连接类异常（`httpx.ConnectError` / `ReadError`） | `"connect"` |
| 其它异常 | `"unknown"` |

> 顺手想一个问题：`429` 和 `401` 该不该计入熔断失败？（答案在 W2 的 B 表第 5 条：不该。但**今天先不做**，记到 Roadmap。）

**② `src/gateway/router.py` — 把原因传到结果里**

`RouteResult` 加一个字段 `failure_kind`，每次失败时更新；全部通道都失败后它保留**最后一次**的失败原因。
另外：所有断路器都打开、一个候选都没有时，标记为 `"all_unavailable"`。

**③ `src/api/completions.py` — 映射成正确的状态码 + 结构化错误体**

| 情况 | 状态码 |
|---|---|
| 成功 | 200（不变） |
| `timeout` | **504** Gateway Timeout |
| `connect` / `upstream_5xx` / `upstream_4xx` / `unknown` | **502** Bad Gateway |
| `all_unavailable`（全熔断/无候选） | **503** Service Unavailable |

错误体统一成：

```json
{
  "error": {
    "code": "upstream_timeout",
    "message": "所有通道均失败：[openai-primary] timeout after 30s",
    "trace_id": "a1b2c3d4",
    "attempted_channels": ["deepseek-primary", "openai-primary"]
  }
}
```

### 验收

```powershell
# 复现/验证脚本：改前应看到 200（bug），改后应看到 502
.venv\Scripts\python.exe experiments\exp5_status_code.py
```

- 改前：`HTTP 200` + 内容里有 `[Error]` / `[Gateway Error]` → 说明 bug 在
- 改后：`HTTP 502`（或 503/504）+ JSON 结构里有 `error.code`

### Day 1 完成记录（证据留档）

**改前**（`experiments\exp5_status_code.py`）

```
HTTP 状态码：200
{"id":"err-...","choices":[{"message":{"content":"[Gateway Error] All connection attempts failed"}}],...}
判定：❌ bug 还在 —— HTTP 200，但内容其实是错误
```

**改后**

```
HTTP 状态码：502
{"error": {"code": "upstream_unreachable", "kind": "connect",
           "message": "[openai-primary] 连接失败: All connection attempts failed",
           "trace_id": "9d9e0d69",
           "attempted_channels": ["deepseek-primary", "openai-primary"],
           "latency_ms": 4064.27}}
判定：✅ 已修复 —— 返回 502，且错误体是结构化的（含 code）
```

**回归验证**

| 测试 | 结果 |
|---|---|
| `python -m py_compile src/**` | 语法 OK |
| `test_failover.py` | 3/3 PASS（exit 0） |
| `tests/test_gateway.py`（需起服务） | 8 PASS + 2 EXPECT（两条"无 Key 路由"用例现在正确返回 502） |
| 审计哈希链 | `Chain intact (58 条记录)` |

**改动文件**：`src/gateway/provider.py`（新增 `UpstreamError`，失败改为抛异常）、
`src/gateway/router.py`（`RouteResult.failure_kind` + 捕获异常 + `all_unavailable`）、
`src/api/completions.py`（状态码映射表 + 结构化错误体 + 审计记录真实状态码）。


**同时必须保证旧功能不坏**：

```powershell
.venv\Scripts\python.exe test_failover.py        # 3/3
.venv\Scripts\python.exe tests\test_gateway.py   # 需先起服务，10/10（其中 502 那条断言正好会变）
```

---

## Day 2–3 · SSE 流式 + 首 token 超时切换

### 设计要点（先想清楚再写）

1. **协议**：`POST /v1/chat/completions` 带 `"stream": true` → 响应 `text/event-stream`
   ```
   data: {"choices":[{"delta":{"content":"你"}}]}
   
   data: {"choices":[{"delta":{"content":"好"}}]}
   
   data: [DONE]
   ```
2. **首 token 超时（关键）**：等第一个 token 最多 `first_token_timeout`（建议 5s，
   与总超时 120s 分开）。**在首个 token 到达之前，客户端什么都没收到 → 可以安全切下一个通道重发**。
3. **切换点只有一个**：`asyncio.wait_for(agen.__anext__(), timeout=first_token_timeout)`。
   一旦拿到第一个 chunk：`return` 出去，开始真流式，**之后不再有切换机会**。
4. **已发出内容后断流**：不能改状态码（200 已发），只能往流里写一个错误事件：
   ```
   data: {"error": {"code": "stream_interrupted", "trace_id": "..."}}
   ```
5. **指标要拆开**：TTFT（首 token 延迟）+ 总时长 + tokens/s。TTFT 同时是健康信号。

### 改动点

| 文件 | 改什么 |
|---|---|
| `provider.py` | 加 `chat_completion_stream()`，用 `client.stream("POST", ...)` 逐行读 `data: ...`，`[DONE]` 结束 |
| `router.py` | 加 `route_stream()`：先逐通道尝试"拿到第一个 chunk"，成功则返回 (通道, 生成器)；全失败则返回错误 |
| `completions.py` | `stream=True` 分支返回 `StreamingResponse`，包一个 `async def gen()` 做透传 + 异常转 error 事件 |
| `config.py` / `providers.yaml` | 新增 `first_token_timeout`（默认 5） |

### 验收

```powershell
.venv\Scripts\python.exe experiments\exp6_stream.py
```
- 正常：逐块打印 token，最后 `[DONE]`，且能看到 TTFT
- 只把主通道打坏：请求仍在 ~5s 内切到备用通道并完整输出
- 全部通道都坏：返回 502（**因为一个 token 都没发出**，状态码还能用）

### Day 2–3 完成记录（证据留档）

```
场景 A · 主通道坏 → 流式请求应切到备用并完整输出
    ← 块0 (+2068ms)  ← 块1 (+2133ms)  ← 块2 (+2213ms)  ← 块3  ← 块4
  chunk 数 : 5（期望 5）    [DONE] : True    TTFT : 2067 ms
  响应头   : channel=openai-primary   attempted=deepseek-primary,openai-primary
  → ✅ 通过

场景 B · 两个通道都坏 → 一个 token 都没发出
  状态码 502 + {"error":{"code":"upstream_unreachable", "attempted_channels":[...]}}
  → ✅ 通过

回归：scripts/regress.py → 语法 PASS / 故障转移 4 场景 PASS / 端到端 8 项 PASS
```

**实现要点（面试讲这四条）**

1. `provider.chat_completion_stream()`：`client.stream()` + `aiter_lines()` 逐行解析 `data:`，`[DONE]` 转内部标记
2. `router.route_stream()`：`asyncio.wait_for(agen.__anext__(), timeout=first_token_timeout)` **抢第一个 chunk**；超时 → `aclose()` + `record_failure()` + 切下一个通道
3. 抢到首个 chunk 即 `record_success()` 并**锁定通道**（客户端已能看到字节，不再切换）
4. `completions._handle_stream()`：零 chunk 时还能用状态码（502/503/504）；一旦开始输出，错误只能写成 `data: {"error": {...}}`

**诚实的边界**：`completions.py` 里那 4 处 `yield` 是照注释补的，属于"会用 + 读过"，不是"从零会写"。面试如实说"这段我读过并理解语义"。


---

## Day 4 · 成本聚合

### 现状

`audit_logs` 表**已经存了** `input_tokens / output_tokens / total_tokens / prompt_version`，
缺的只是"计价 + 聚合"。

### 改动点

1. `config/pricing.yaml`（新建）：provider × model → 每百万 token 单价
   ```yaml
   deepseek:
     deepseek-chat:  { input: 2.0,  output: 8.0 }
     deepseek-reasoner: { input: 4.0, output: 16.0 }
   openai:
     gpt-4o-mini:    { input: 1.1,  output: 4.4 }
   ```
   （单价写成"每百万 token"，和各家官网口径一致，避免小数点误差）
2. `src/audit/store.py`：加 `usage_summary(group_by="provider")` 聚合查询
3. `src/api/audit.py`（或新开 `api/usage.py`）：`GET /v1/usage?group_by=provider|model|day`，返回
   ```json
   {"total_cost": 0.0123, "total_tokens": 45678, "breakdown": [{"key":"deepseek","cost":0.008,"tokens":30000,"requests":12}]}
   ```
4. `static/dashboard.html`：加一张成本卡片（可选，但截图给面试官看很加分）

### 验收

- `/v1/usage` 的数字与自己手算一致（拿 3 条记录验算）
- 缓存命中的请求成本记 0（或单独标记），不能算成一次完整调用

### Day 4 完成记录（证据留档）

```
$ python experiments/exp7_usage.py
  ── group_by=model  HTTP 200 ──   total_cost=¥0.019628  cost_estimated=False
      gpt-4o-mini     tok=3000  cost=¥0.006600      ← 与脚本独立手算完全一致
      deepseek-chat   tok=1700  cost=¥0.007000      ← 同上
  ── group_by=provider HTTP 200 ── total_cost=¥0.034160  cost_estimated=True
  ── group_by=day      HTTP 200 ── total_cost=¥0.034160  cost_estimated=True
  结论：✅ 成本聚合通过
```

**实现要点**

1. `audit_logs` 早已存 `input/output/total_tokens`，所以成本只需"聚合 SQL + 单价表"，不新增埋点
2. 三个视图（provider / model / day），`group_by` 走**白名单**映射，`since` 用 `?` 参数化（防注入）
3. 成本公式在 API 层：`in/1e6*单价in + out/1e6*单价out`，单价表 `config/pricing.yaml` 可改不改码
4. **诚实的取舍**：只有 `group_by=model` 能精确定价（需要 provider+model 两个维度）；provider/day 视图返回 `cost_estimated: true`

**踩到并修掉的一个真实坑（值得讲）**

`exp7_usage.py` 第一版往审计表种数据时，链断在种子记录上——原因有两个：
① 种子传的 `latency_ms` 是 `int`，而 SQLite 的 `REAL` 列读回来是 `float`，`json.dumps(800)` ≠ `json.dumps(800.0)`，重算哈希必然不一致；
② 更根本的：**两个进程同时 append 审计记录**，各自读到同一个 `last_hash` 再各自写入 → 分叉。
修复用 `exp7_usage.py --repair`：从断点截断 + 同步 `audit_meta.last_hash`（这正是 W1 实验 3 里"尾部删除查不出"的反向利用）。
**结论**：审计写入必须**单写者**（或每实例独立链后汇总），这是哈希链方案的固有约束。


---

## W3 完成后的"可讲清单"

- [ ] 我把"上游失败返回 200"改成了 502/504/503 + 结构化错误体，并说明为什么这件事对监控/重试是致命的
- [ ] 我实现了 SSE 流式，并把故障切换压缩到"首个 token 之前"的窗口里，用首 token 超时兜住
- [ ] 我知道流式之后错误不能再变成 500，只能走流内 error 事件
- [ ] 我做了成本聚合，能按 provider/model 报出 token 与金额

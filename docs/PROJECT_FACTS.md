# 项目事实卡（读这一个文件 ≈ 读过全部源码）

> 用途：让 AI/新同事 30 秒掌握全貌，避免每次重读 20 个源文件。

## 命令

| 做什么 | 命令 |
|---|---|
| 跑回归（语法+单测+端到端） | `.venv\Scripts\python.exe scripts\regress.py` |
| 只跑语法+单测 | `.venv\Scripts\python.exe scripts\regress.py --skip-e2e` |
| 起服务 | `.venv\Scripts\python.exe src\main.py`（:8080，仪表盘 `/`） |
| 断路器判分 | `.venv\Scripts\python.exe experiments\w2_breaker_check.py` |
| 故障转移实验（mock 备用） | `.venv\Scripts\python.exe experiments\exp_failover_live.py` |
| 状态码实验（改前/改后证据） | `.venv\Scripts\python.exe experiments\exp5_status_code.py` |

## 数据流

```
Client → auth(401) → rate_limit(429) → body(400) → prompt(400) → cache
      → router(别名→候选通道→LB→断路器→provider→失败重试) → audit(哈希链)
```

## 模块职责

| 文件 | 职责 | 关键接口 |
|---|---|---|
| `src/config.py` | YAML 配置 + 别名/通道/model_map 解析 | `config.load()`、`find_channels_for_model()` |
| `src/gateway/router.py` | 路由 + 故障转移 + `failure_kind` | `router.route(req) -> RouteResult` |
| `src/gateway/provider.py` | 上游 HTTP 调用；**失败抛 `UpstreamError(kind,...)`** | `adapter.chat_completion()` |
| `src/gateway/circuit_breaker.py` | 三态断路器（**W2 由本人重写**） | `can_execute() / record_*()`、`state` 属性惰性晋升 |
| `src/gateway/load_balancer.py` | 优先级分组内加权轮询等 4 策略 | `select(candidates)` |
| `src/api/completions.py` | 管道组装 + **kind→状态码映射** + 结构化错误体 | `STATUS_BY_KIND`、`ERROR_CODE_BY_KIND` |
| `src/audit/store.py` | SQLite + SHA-256 链、`verify_chain()`、`stats()` | 表 `audit_logs`（已存 input/output/total_tokens） |
| `src/prompt/registry.py` | 提示词版本化 + 加权 A/B + Jinja2 | `resolve(id, vars)` |
| `src/middleware/*` | auth(fail-close) / rate_limit(fail-open) | — |

## 关键事实

- `provider.chat_completion` 成功返回 `(resp, latency_ms, True)`；**失败抛 `UpstreamError`**（不再返回错误对象）
- 状态码映射：`timeout→504`、`connect/5xx/429/auth/4xx/unknown→502`、`all_unavailable→503`
- 断路器：`failure_threshold=5`、`recovery_timeout=60`、`half_open_max_calls=1`；`>=` 触发熔断
- 审计里 `error` / `api_key` **不在哈希链内**（盲区，改这两列查不出）

## 已知坑

1. IDE 会自动导入垃圾库（`from sympy import true`、`from PIL.Image import OPEN`）→ 见到就删
2. hash_data 只含 10 个字段，改 `error` 不会被 `verify_chain` 发现
3. 未实现：SSE 流式、成本聚合、Docker（W3/W4 待做）

# LLM Gateway — 大模型智能网关

> OpenAI 兼容的大模型网关：多提供商路由 · 故障转移 · 限流 · 缓存 · 提示词 A/B · 防篡改审计。
> Python / FastAPI，`src/` 约 1700 行，**不基于 LiteLLM / Portkey / One-API 等成品网关**。
> 架构参考多款开源网关的思路，代码在 AI 辅助下组装改造；**断路器、路由故障转移两块已由我逐行读懂并亲手重写**（见 [`docs/W2_REWRITE.md`](docs/W2_REWRITE.md)）。

---

## 项目来源与我的改造（如实说明）

这个项目不是从零手写的。做法是：分析几款开源网关（LLMGov、AIGC Gateway、Helicone、PromptShield、Portkey）的公开架构 → 设计自己的模块划分 → AI 辅助组装代码 → 我逐模块读、跑通、发现问题、改造。

**我做的部分**（会持续更新，这是面试时最该讲的东西）：

| 状态 | 内容 |
|---|---|
| ✅ 已完成 | 逐个模块读懂 8 个核心机制，能画出请求管道与路由子流程 |
| ✅ 已完成 | 跨提供商故障转移（`model_map` 虚拟模型名 → 通道级真实模型），并修复了「负载均衡不分 priority 组导致 50% 流量打到备用通道」的问题 |
| 🔄 进行中 | 断路器模块从零重写（状态机 + 并发保护 + 探测配额），用原 mock 测试做回归 |
| 📋 待办 | SSE 流式 + 首 token 超时切换；成本聚合；修复「上游失败仍返回 HTTP 200」的状态码设计问题 |
| 📋 待办 | Docker 部署 + 并发压测 |

> 为什么如实写：面试官认得这些开源项目。说"从零手写"是一票否决；说"架构借鉴 + 这两块我亲手重写 + 我发现了这个设计缺陷"，才是有分量的版本。

---

## 这个项目解决什么问题

企业内部接大模型，真正难的从来不是"调通 API"，而是这四件事：

1. **多家模型混用** —— 主模型便宜、备用模型稳、不同业务要用不同模型，谁来做统一入口和统一模型名？
2. **上游会挂** —— 供应商超时、5xx、限流是常态，业务代码不能每处都写重试和降级。
3. **成本不可见** —— 哪个业务、哪个模型花了多少钱，出问题能不能追溯？
4. **审计与合规** —— 政企/金融场景要求调用记录可追溯、不可篡改。

这个网关把上述四件事收敛到一层：业务侧只写"我调 `auto`"，网关负责选谁、失败了换谁、花了多少、留什么证据。

---

## 30 秒电梯演讲（可直接照念）

> "这是一个大模型网关，对外提供 OpenAI 兼容接口。它做了三件生产环境必须、但业务代码不该承担的事：
> 第一，**跨提供商路由与故障转移**——客户端请求虚拟模型名 `auto`，网关按优先级先走 DeepSeek，一旦超时或报错就自动切到 OpenAI，连续失败 5 次进熔断，冷却 60 秒后半开探测自动恢复，业务侧完全无感；
> 第二，**成本与审计**——每次调用落库 token 用量、延迟、通道、是否缓存命中，审计表用 SHA-256 哈希链串起来，任何一条被改动都能被检出；
> 第三，**治理能力**——按 Key 的滑动窗口限流、响应缓存、提示词版本化与加权 A/B。
> 架构是我分析了几款开源网关之后设计的，代码在 AI 辅助下组装，但断路器、路由故障转移这两块是我逐行读懂并亲手重写的；过程中我还发现了一个上游失败仍返回 200 的状态码设计缺陷。所以这套系统的取舍我讲得清，也知道哪里做得不够——生产环境我会换成 LiteLLM + Redis/PostgreSQL，自己只保留差异化的那层。"

---

## 架构

### 请求管道

```
  OpenAI SDK / curl / 业务服务
              │
              ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  POST /v1/chat/completions                                   │
   ├──────────────────────────────────────────────────────────────┤
   │ ① Auth         API Key 校验        —— 失败必须拒绝（fail-close）│
   │ ② RateLimit    滑动窗口 / Key 维度  —— 自身故障降级放行（fail-open）│
   │ ③ PromptResolve 版本 + 加权 A/B + Jinja2 渲染                 │
   │ ④ Cache        SHA-256(请求) + TTL 命中直接返回                │
   │ ⑤ Router       别名 → 候选通道 → LB → 断路器 → 调用 → 失败重试  │
   │ ⑥ Audit        异步落库：token / 延迟 / 通道 / 次数 / 哈希链    │
   └──────────────────────────────────────────────────────────────┘
              │
              ▼
   响应体 + x_gateway 元信息（trace_id / provider / channel /
                            attempted_channels / latency_ms）
```

### 路由与故障转移（核心）

```
请求 model="auto"
   │  model_aliases:  auto → auto-chat
   ▼
候选通道按 priority 分组：deepseek-primary(1,weight 100) / openai-primary(2,weight 100)
   │
   ▼
┌─ loop（最多 N 次，N = 候选通道数）────────────────────────┐
│  LB 选择（仅在最低 priority 组内轮询，保证"主备语义"）      │
│       ↓                                                  │
│  断路器 can_execute? ── No ──→ 移除该通道，continue        │
│       ↓ Yes                                              │
│  通道级 model_map 翻译：auto-chat → deepseek-chat / gpt-4o-mini │
│       ↓                                                  │
│  Provider 调用 ── 成功 ──→ break（记录 success + 延迟样本） │
│       └──────── 失败 ──→ record_failure，移除该通道，重试    │
└──────────────────────────────────────────────────────────┘
```

### 断路器状态机

```
CLOSED ──(连续失败 ≥ 5)──▶ OPEN ──(冷却 60s)──▶ HALF_OPEN ──(探测成功)──▶ CLOSED
                                                     └──(探测失败)──▶ OPEN
```

### 防篡改审计哈希链

```
Record n:  data_n | prev_hash = H(n-1) | curr_hash = H(n)
H(n) = SHA256( H(n-1) + canonical_json(data_n) )
```

任意一条记录被修改 → 其 `curr_hash` 变化 → 后续所有记录的 `prev_hash` 对不上 → `/v1/audit/verify` 检出断链位置。

---

## 核心设计取舍

| 决策 | 选择 | 代价 / 为什么 | 不这么做会怎样 |
|---|---|---|---|
| 故障转移实现 | 循环 + 候选移除 | 天然支持 N 个通道，加通道只改 YAML | try/except 嵌套只能处理两三个，且无法表达"每个通道只试一次" |
| 负载均衡 | 只在最低 priority 组内轮询 | 牺牲一部分"全量均衡"，换来主备语义 | 纯加权轮询会把 50% 流量打到备用通道，成本翻倍（**这是实际踩到的 bug**） |
| 模型名 | 通道级 `model_map` 虚拟模型 | 多一层映射，调试要多看一行日志 | 跨提供商模型名无法统一，客户端得自己记住各家真实模型名 |
| 审计落库 | 异步写 + 哈希链 | 内存队列丢失风险（生产应换 MQ/PostgreSQL） | 同步写阻塞热路径，p99 直接劣化 |
| 认证失败 | fail-close（拒绝） | 可用性让位于安全 | 认证组件故障时任何请求都能穿透 |
| 限流失败 | fail-open（放行） | 限流组件故障时可能被打穿 | 保护者变成单点故障，整个网关不可用 |
| 断路器状态 | `@property` 惰性转换 | 无后台定时器线程 | 需要定时器扫表，复杂且浪费 |
| 与成品网关关系 | 自研核心 + 借鉴成熟存储方案 | 功能覆盖不如 LiteLLM | 直接用成品 → 无法讲清"为什么"，作为工程证明的价值归零 |

---

## 快速开始

```bash
cd llm-gateway
python -m venv .venv
# Windows
.venv/Scripts/pip install -r requirements.txt
# Linux / macOS
.venv/bin/pip install -r requirements.txt
```

配置 API Key（**`config/providers.yaml` 里只有 `${ENV}` 占位，仓库无任何明文密钥**）：

```bash
# Windows PowerShell
$env:DEEPSEEK_API_KEY="sk-..."; $env:OPENAI_API_KEY="sk-..."
# Linux / macOS
export DEEPSEEK_API_KEY="sk-..."; export OPENAI_API_KEY="sk-..."
# 可选：开启网关侧鉴权
export GATEWAY_API_KEYS="key-1,key-2"
```

启动：

```bash
python src/main.py          # 默认 http://0.0.0.0:8080
```

调用（把 `base_url` 指向网关即可，业务代码零改动）：

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8080/v1", api_key="any")

# 请求虚拟模型 auto：优先 DeepSeek，挂了自动切 OpenAI
resp = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
print(resp.x_gateway["attempted_channels"])   # 例如 ['deepseek-primary', 'openai-primary']
```

---

## 90 秒演示脚本（面试现场照着放）

| 时间 | 动作 | 说明要点 |
|---|---|---|
| 0–15s | 打开 `http://localhost:8080` 仪表盘 | "这是通道健康、断路器状态、审计日志的实时视图" |
| 15–40s | 发一条 `model: "auto"` 请求 | 展示响应里的 `x_gateway.provider = deepseek`，说明主通道生效 |
| 40–70s | 把 `config/providers.yaml` 里 DeepSeek 的 `base_url` 改错（或用无效 Key），重启后再发一次 | 展示 `attempted_channels = ['deepseek-primary','openai-primary']`，说明自动切换 |
| 70–90s | 连发 5 次触发熔断，看仪表盘断路器变 OPEN、冷却 60s 后 HALF_OPEN | "熔断期间不再浪费一次超时，直接走备用；恢复后自动切回" |

---

## 已验证 / 未完成（诚实清单）

**已验证**（本机 Python 3.13 实测，2026-10-19）

- `python -m py_compile src/**/*.py`：**全部通过**
- `python test_failover.py`：**3/3 PASS**（mock，无需真实 Key）—— 主通道正常 / 主通道故障自动切备用 / 连续失败 5 次熔断后直连备用
- `python tests/test_gateway.py`：**10/10 通过**（需先启动服务）—— 健康检查、模型列表、网关状态、提示词列表/渲染、路由链路、审计日志、**哈希链完整性 `Chain intact (24 条记录)`**、审计统计

**已知问题（下一步要修的）**

- ⚠️ **上游失败仍返回 HTTP 200**：`provider.py` 把上游错误包成"正常的响应体"，业务侧只能靠响应内容判断失败，无法用状态码做重试。应改为 502/504 + 结构化错误体。
- ❌ **SSE 流式输出**（含"首 token 超时即切换通道"——流式下的故障转移窗口与非流式完全不同，是个有意思的问题）
- ⚠️ **成本聚合**：audit 表已落 `input/output/total_tokens`，但缺"按 provider×model 计价 + 预算控制"的聚合层
- ❌ Docker / docker-compose 部署、压测报告（并发、p95 延迟）
- ❌ 语义缓存、Guardrails、虚拟 Key 预算
- ❌ 流式仪表盘开关、Prometheus 指标

---

## 为什么不用 LiteLLM / Portkey？

> 这是学习型项目，目标是把核心机制亲手实现一遍以理解原理 —— 路由循环、断路器状态机、哈希链这些底层逻辑，用框架是黑盒，自研才能讲透"为什么"、"不这么做会怎样"。它零外部依赖、体量小、可随意改造。
> 生产环境我会做组合：**自研网关（或直接上 LiteLLM）+ 成熟存储与缓存组件（Redis / PostgreSQL）**，而不是继续手写存储层。判断一个团队该自研还是买现成的，看的是"差异化在哪"——如果网关能力本身就是产品差异化就自研，否则用现成的，把精力放在业务上。

| 能力 | 本项目 | LiteLLM | Portkey |
|---|---|---|---|
| 多提供商路由 | ✅ 自研 | ✅ 100+ | ✅ 1600+ 模型 |
| 故障转移 / 断路器 | ✅ | ✅ fallback | ✅ |
| 虚拟模型名映射 | ✅ `model_map` | ✅ model alias | ✅ |
| 提示词版本化 + A/B | ✅ 加权随机 | 部分 | ✅ |
| 防篡改审计链 | ✅ SHA-256 链 | ❌ | 部分 |
| token 成本聚合 / 预算 | ⚠️ 待做 | ✅ virtual keys | ✅ |
| 语义缓存 | ❌ | ✅ | ✅ |
| 流式 | ❌ 待做 | ✅ | ✅ |
| Guardrails | ❌ | 部分 | ✅ |

---

## Roadmap（按优先级）

- [ ] **P0 · 上游失败的状态码语义**：失败返回 502/504 + 结构化错误体，不再用 200 包错误
- [ ] **P0 · SSE 流式输出** + 首 token 超时切换通道（AI 应用岗高频考点）
- [ ] **P0 · 成本聚合**：`pricing.yaml`（provider×model 单价）→ `/v1/usage` 汇总 → 仪表盘成本卡片 → 按 Key 预算
- [ ] **P0 · Docker 部署 + 压测报告**（100 并发，QPS / p95 / 错误率）
- [ ] P1 · `/metrics` Prometheus（qps / latency_p99 / error_rate / 断路器状态）
- [ ] P1 · 语义缓存（embedding + cosine，命中"你好"与"hello"）
- [ ] P2 · Guardrails（敏感词 / 越权 / 输出结构校验）
- [ ] P2 · 存储层替换（SQLite → PostgreSQL + Redis），支持多实例水平扩展

---

## 目录结构

```
llm-gateway/
├── src/
│   ├── main.py                  # 应用入口（lifespan 初始化各模块）
│   ├── config.py                # 配置加载 + 别名/通道/model_map 解析
│   ├── models/schemas.py        # Pydantic 请求/响应模型
│   ├── gateway/
│   │   ├── router.py            # ⭐ 路由 + 故障转移循环
│   │   ├── load_balancer.py     # 4 种策略 + 按 priority 分组
│   │   ├── circuit_breaker.py   # ⭐ 三态状态机 + asyncio.Lock
│   │   └── provider.py          # 上游 HTTP 适配器
│   ├── prompt/registry.py       # 提示词版本化 + 加权 A/B + Jinja2
│   ├── audit/
│   │   ├── logger.py            # 异步审计记录
│   │   └── store.py             # ⭐ SQLite + SHA-256 哈希链
│   ├── middleware/
│   │   ├── auth.py              # API Key 认证（fail-close）
│   │   └── rate_limit.py        # 滑动窗口（fail-open）
│   ├── cache/response_cache.py  # 请求哈希缓存 + TTL
│   └── api/                     # completions / health / prompts / audit 端点
├── config/
│   ├── providers.yaml           # 提供商、通道、model_map、限流与 LB 策略
│   └── prompts.yaml             # 提示词注册表（版本 + 权重 + 变量）
├── static/dashboard.html         # 监控仪表盘（通道健康 / 断路器 / 审计）
├── tests/test_gateway.py        # 单元测试
├── test_failover.py             # ⭐ 故障转移场景验证（mock，无需真实 Key）
├── demo_assistant.py            # 端到端调用示例
├── experiments/                 # 学习型实验脚本（优先级分组、model_map、校验）
└── docs/ANALYSIS.md             # 开源项目对标分析（LLMGov / AIGC Gateway / Portkey …）
```

### API 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI 兼容聊天补全 |
| `/v1/models` | GET | 可用模型列表 |
| `/v1/status` | GET | 网关完整状态（通道健康 / 断路器 / 策略） |
| `/v1/prompts`、`/v1/prompts/{id}` | GET | 提示词列表 / 详情 |
| `/v1/prompts/{id}/dry-run` | POST | 仅渲染模板，不调模型 |
| `/v1/audit/logs`、`/v1/audit/stats` | GET | 审计日志查询 / 统计 |
| `/v1/audit/verify` | GET | 哈希链完整性验证 |
| `/health` | GET | 健康检查 |

---

## 参考与致谢

架构设计参考了以下开源项目的公开实现思路，代码为独立编写：

- **LLMGov**（Python/FastAPI）—— 断路器、防篡改审计链
- **AIGC Gateway**（Next.js/TypeScript）—— 优先级路由、提示词工作流
- **Helicone Gateway**（Rust）—— P2C 负载均衡
- **PromptShield Gateway**（Go）—— 限流、审计日志
- **Portkey AI Gateway** —— 模型路由、Guardrails

对标分析细节见 [`docs/ANALYSIS.md`](docs/ANALYSIS.md)。

## License

MIT

# W2 · 亲手重写断路器（60–80 行）

> **为什么是这一块**：你告诉我项目是「从多个开源项目读取后交给 AI 改的，你运行过」，而你**会写 SQL、会跑脚本**。
> 这个组合意味着两件事：① 你的强项是"数据 + 执行"，不是"从零搭框架"，所以重写整条路由链对你是浪费；② 断路器是**纯逻辑、零外部依赖、能用一个脚本判定对错**的模块——它是最适合你建立"这段代码我写的"这种实感的地方。
> 写完之后，`test_failover.py`（3 个故障场景）能继续通过，你就有第一个"亲手改过"的证据。

**总投入**：4 天 × 3 小时 = 12 小时。做完再进 W3。

---

## 规则（这三条决定这件事有没有价值）

1. **写的时候不许打开 `src/gateway/circuit_breaker.py`。** 写完后可以打开做 diff——顺序反过来，效果完全不同。
2. **可以用 AI 查语法、问概念**（"asyncio.Lock 怎么用""Enum 怎么定义"），**但不许让 AI 一次性生成整个类**。每次只问一个点，问完自己敲。
3. **每一行你都要能用自己的话念出来。** 念不出来的行就删掉重写——面试官追问时，你嘴里的速度就是你对它的熟悉度。

---

## 接口规格（验收器认这四个名字，签名必须一致）

文件名：`experiments/w2_breaker.py`

```python
class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class CircuitBreaker:
    def __init__(self, name="default", failure_threshold=5, recovery_timeout=60, half_open_max_calls=1): ...
    @property
    def state(self): ...            # 返回 CircuitState；读取时若冷却已到期，要自动变成 HALF_OPEN
    async def can_execute(self) -> bool: ...
    async def record_success(self): ...
    async def record_failure(self): ...
    def to_dict(self) -> dict: ...  # 至少含：name / state / failure_count / failure_threshold / recovery_timeout

class CircuitBreakerManager:
    def get(self, channel_id: str) -> CircuitBreaker: ...   # 同一 id 返回同一实例
    def all_status(self) -> list[dict]: ...

breaker_manager = CircuitBreakerManager()
```

> 接口为什么必须一致：`src/gateway/router.py` 里写死了 `breaker_manager.get(channel_id)` + `await cb.can_execute()` + `await cb.record_success/record_failure()`。你按接口写，就能直接替换进去跑真实实验。

---

## 行为规格（9 条，验收器逐条判）

| # | 行为 | 注意点 |
|---|---|---|
| 1 | 初始状态 `closed`，`can_execute()` 返回 `True` | — |
| 2 | 连续失败达到 `failure_threshold`（=5）时从 `closed` 转 `open` | 第 4 次必须还是 closed |
| 3 | `open` 期间 `can_execute()` 返回 `False` | 不要抛异常，返回布尔 |
| 4 | 冷却 `recovery_timeout` 到期后自动转 `half_open`，并**只放行 1 个**探测请求（`half_open_max_calls=1`） | 第 2 个探测必须被拒 |
| 5 | `half_open` 下探测成功 → 回 `closed`，失败计数清零 | `to_dict()` 里 `failure_count` 要变 0 |
| 6 | `half_open` 下探测失败 → 回 `open`，**冷却重新计时**（刚失败后立刻再问必须被拒） | 这一条最容易漏：忘了刷新失败时间戳 |
| 7 | `closed` 下一次成功要把失败计数清零 | 否则"4 次失败 + 1 次成功 + 1 次失败"会误熔断 |
| 8 | Manager：同一 channel 返回**同一实例**；不同 channel **互不影响**；`all_status()` 返回列表 | 这里是最常见的 Python 默认参数/共享状态坑 |
| 9 | 并发调用 `can_execute()` 不应出错（20 个并发全部放行） | 用 `asyncio.Lock` 保护状态读写 |

**一个隐藏要求**：状态转换要打日志（`logger.info/warning`），因为实验 2 里你是靠日志确认熔断发生的。

---

## 开发步骤

### Day 1（3h）· 骨架 + 前 3 条
1. 建 `experiments/w2_breaker.py`，写 `CircuitState` 和 `CircuitBreaker.__init__`（只需 6 个字段：name、threshold、recovery_timeout、half_open_max_calls、当前状态、失败计数、最后失败时间、探测计数）。
2. 实现 `state` 属性和 `can_execute()`，先**不要**看第 4/6 条的自动转换——就用最笨的办法：open 且没到期 → False；到期 → 改成 half_open。
3. 跑验收器，前 3 条应全绿：
   ```powershell
   .venv\Scripts\python.exe experiments\w2_breaker_check.py
   ```

### Day 2（3h）· 状态迁移 + 第 4–7 条
1. 加 `record_success` / `record_failure`，把状态机补全。
2. 重点攻第 6 条（探测失败要刷新冷却计时）和第 7 条（closed 下成功清零）。
3. 再跑验收器，目标是全部通过（9 组 / 16 条断言，全绿）。

### Day 3（3h）· 接进真实系统
1. 备份原文件：`Copy-Item src\gateway\circuit_breaker.py src\gateway\circuit_breaker.py.orig`（留着做 diff）。
2. 把你的实现拷进 `src/gateway/circuit_breaker.py`（**只需保证四个名字都导出**），跑：
   ```powershell
   .venv\Scripts\python.exe test_failover.py     # 目标：3/3 通过
   ```
3. **重做实验 2**（把 deepseek 的 base_url 改坏，连发 6 次），确认日志里出现 `CLOSED → OPEN`，第 6 次的 `attempted_channels` 只剩 `openai-primary`。
   → 这一刻你就有资格说"这个熔断机制是我实现的"。

### Day 4（3h）· diff + 讲稿
1. 打开 `circuit_breaker.py.orig` 做对比（VS Code 里直接比）。
2. 填下面这张表——**这是你面试的讲稿素材**，比任何背诵都有用：

| 对比项 | 原版怎么做 | 我怎么做 | 谁的更好 / 为什么 |
|---|---|---|---|
| OPEN → HALF_OPEN 怎么触发 | | | |
| 并发保护 | | | |
| 探测配额 | | | |
| 探测失败后的冷却 | | | |
| 计数清零时机 | | | |

3. 回答三个必被追问的问题（写下来）：
   - **"如果 threshold 设成 1 会怎样？"**（提示：一次抖动就熔断，恢复靠什么撑）
   - **"如果 60 秒内所有请求都失败，你的断路器会不会一直 open？"**（提示：谁把它变回 half_open、什么时候）
   - **"多进程部署时这套断路器还有效吗？"**（提示：状态在内存里，这是个真缺陷——生产要用 Redis 共享状态。**承认它，比硬答好得多**）

---

## 卡住时的三问（别急着问 AI 要代码）

1. 这个状态是"发生事件"时改的，还是"读取时"改的？我的写法会不会漏掉一条路径？
2. 这个字段有没有可能在两个地方被同时改？（有 → 需要锁）
3. 如果我现在只发 **1 个** 请求，它会走哪条分支？从入口顺着念一遍。

---

## 验收

```powershell
.venv\Scripts\python.exe experiments\w2_breaker_check.py   # 16/16 全绿
.venv\Scripts\python.exe test_failover.py                  # 3/3
```

两条都绿 → W2 通过，我们进 W3（SSE 流式 + 成本聚合 + 修那个 HTTP 200 的 bug）。

---

## 额外提醒：你的项目来源，要如实说

面试时说"这是我自己从零写的"，一旦对方认得 LLMGov / Portkey 的代码结构，就结束了。正确说法反而更有分量：

> "架构是我分析了几款开源网关之后设计的，代码在 AI 辅助下组装，但**断路器、路由故障转移这两块是我逐行读懂并且亲手重写的**，而且我发现并修掉了一个上游失败仍返回 200 的状态码设计问题。"

这句话同时给了三样东西：**诚实、动手证据、工程判断力**。这也是我在 W3 让你修那个 bug 的原因——它比十个功能点更能证明"真的读过代码"。

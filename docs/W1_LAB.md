# W1 理解攻坚 · 5 个动手实验

> 目标：不是背题，是**通过"把系统搞坏"来真的懂它**。每做完一个实验，你就能用自己的话回答一组问题——那才是面试里你会被问的东西。
> 总耗时：约 3.5 小时（5 个实验）。做不完分两天做，别跳。
> 背景：上一轮你答不上那 5 道题，那说明的是"项目不是你的"，不是"你不行"。这 5 个实验就是把它变成你的最快路径——**改一行看一次行为变化，比读十遍代码有效**。

---

## 准备工作（5 分钟，必须做）

```powershell
cd D:\桌面\周报\ai\2026-08-10-10-06-37\llm-gateway

# 1. 备份配置——每个实验都要改它，改坏了能还原
Copy-Item config\providers.yaml config\providers.yaml.bak -Force

# 2. 备份数据库（实验 3 要故意篡改它）
Copy-Item data\gateway.db data\gateway.db.bak -Force

# 3. 启动网关（单独开一个窗口，别关）
.venv\Scripts\python.exe src\main.py
```

另开一个窗口做实验，用这条命令发请求（模型名保持 `auto`）：

```powershell
function Ask-Gateway {
    param([string]$Model = "auto")
    $body = @{ model = $Model; messages = @(@{ role = "user"; content = "你好" }) } | ConvertTo-Json -Depth 5
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:8080/v1/chat/completions" -Method Post -Body $body -ContentType "application/json"
        "{0} | provider={1} | channel={2} | attempted={3}" -f $r.x_gateway.trace_id, $r.x_gateway.provider, $r.x_gateway.channel, ($r.x_gateway.attempted_channels -join " → ")
    } catch {
        "HTTP {0}: {1}" -f $_.Exception.Response.StatusCode.value__, $_.ErrorDetails.Message
    }
}
```

> 说明：你现在**没有配 API Key**，所以每次上游调用都会失败——没关系，**故障正是这 5 个实验的主角**。观察点主要是 `attempted_channels` 的**顺序**和服务端日志，而不是回答内容。

**每个实验的产出**：3 行笔记（`解决了什么问题 / 怎么实现的 / 为什么这么设计`）+ 回答问题。写到 `docs/NOTES_W1.md`。

---

## 实验 1 · 换个优先级，看谁先被接进来（20 分钟）

**理解目标**：`model_aliases` / `model_map` / `priority` 三者分别在路由的哪一步生效。

**操作**：把 `config/providers.yaml` 里两个通道的 `priority` 对调（deepseek 改 2、openai 改 1），保存后重启网关，用 `Ask-Gateway` 发一次请求。

**观察**：
- `attempted = ['openai-primary' → 'deepseek-primary']`（原来顺序相反）
- 日志里出现 `Routing to openai/openai-primary (model=gpt-4o-mini, attempt=1/2)`
- 打开 `http://127.0.0.1:8080/v1/status`，看通道的排序和权重

**要回答的问题**：
1. 请求的是 `auto`，最后发给上游的却是 `deepseek-chat` 或 `gpt-4o-mini`。这两次"改名"分别发生在哪个文件、哪一步？
2. 如果删掉 `model_aliases` 里的 `"auto": "auto-chat"` 这一行，会发生什么？先猜，再试。
3. 为什么 `model_map` 要做成"每个通道各存一份"的映射，而不是全局一张表？

**通过标准**：能画出"客户端模型名 → 别名 → 通道 → 上游真实模型名"这条链，并指出每一跳在哪个函数的哪一行。

---

## 实验 2 · 让主通道挂掉，看它怎么自救（40 分钟）

**理解目标**：故障转移循环 + 断路器三态。

**操作**：
1. 还原配置（`Copy-Item config\providers.yaml.bak config\providers.yaml -Force`），然后把 deepseek 的 `base_url` 改成 `http://127.0.0.1:9/v1`（一个必定连不上的地址），重启。
2. 发一次请求，记下 `attempted_channels`。
3. **连续发 6 次**，盯着服务端日志。

**观察**：
- 前 5 次：`attempted = ['deepseek-primary' → 'openai-primary']`，日志里每次都有 `[deepseek-primary] Request failed (attempt 1), trying next channel...`
- 第 5 次之后出现：`[deepseek-primary] Circuit CLOSED → OPEN (failures=5)`
- **第 6 次**：`attempted = ['openai-primary']`（不再尝试 deepseek），日志里是 `Circuit breaker open, skipping`
- 再等 60 秒发一次，会看到 `HALF_OPEN` 探测

**要回答的问题**：
1. 失败后为什么要把通道"从候选列表里移除"，而不是直接 `continue` 重试？如果只是重试会死循环吗？
2. 熔断期间跳过 deepseek，实际省下了什么？（提示：想一次超时的代价）
3. 60 秒后第一次请求会发生什么？如果它又失败了，状态怎么走？
4. 如果两个通道都熔断，用户看到的是什么？（`select()` 返回 `None` 的那条分支）

**通过标准**：能白板画出三态图，并说清每一根箭头的触发条件来自哪行代码。

---

## 实验 3 · 篡改审计库，看它能不能发现（40 分钟）

**理解目标**：哈希链到底防什么、防不了什么。

**操作**：

```powershell
# 先确保有记录（发几次请求），然后直接改数据库
.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/gateway.db'); c.execute(\"UPDATE audit_logs SET provider='hacked' WHERE id=(SELECT MIN(id) FROM audit_logs)\"); c.commit(); print('已篡改 id 最小的一条')"

# 验证
Invoke-RestMethod -Uri "http://127.0.0.1:8080/v1/audit/verify"
```

**观察**：返回 `valid=false`，消息里指出断链的位置（第几条）。

**然后做三个对照实验**（这是本实验的精华）：

| 篡改方式 | 你猜能不能被发现 | 实际结果 |
|---|---|---|
| 改第 1 条的 `provider` | ? | |
| 删掉**最后一条**记录 | ? | |
| 删掉**中间**一条记录 | ? | |
| 改第 1 条的 `error` 字段 | ? | |
| 改第 1 条的 `api_key` 字段 | ? | |

> 提示：去看 `src/audit/store.py` 第 112–123 行的 `hash_data` 里到底包含哪些字段，再看 `verify_chain` 是怎么从 `"0"*64` 开始逐条比对的。

**要回答的问题**：
1. 为什么改一条记录会让**后面全部**对不上？
2. 删掉最后一条能被发现吗？如果不能，现实里该怎么补救？（提示：外部锚定）
3. `error` 和 `api_key` 没被纳入哈希，这是什么性质的问题？会造成什么后果？

**通过标准**：能说出"哈希链保证的是历史不可改、且顺序可验"，以及它的**两个盲区**（尾删、未纳入哈希的字段）。

---

## 实验 4 · 把自己限流住（25 分钟）

**理解目标**：滑动窗口 + fail-open 的取舍。

**操作**：把 `config/providers.yaml` 里 `default_rpm` 改成 `3`，重启，然后连发 5 次（同一个窗口内）：

```powershell
1..5 | ForEach-Object { Ask-Gateway }
```

**观察**：第 4、5 次返回 `HTTP 429`，响应头里有 `Retry-After`。

**要回答的问题**：
1. 业务侧收到 429 应该怎么做？为什么不能简单"立即重试"？
2. `middleware/rate_limit.py` 里如果内部统计出错，为什么会**放行**（fail-open）而不是拒绝？这跟认证的 fail-close 是相反的选择，为什么？
3. 滑动窗口和固定窗口（每分钟清零）有什么区别？哪个更公平？

**通过标准**：能讲清"保护组件自己不能变成单点故障"这条原则，并能举出反例（认证为什么必须反过来）。

---

## 实验 5 · 抓一个真实的 bug：失败了还返回 200（30 分钟）

**理解目标**：错误语义设计——这是我读代码时发现的真问题，也是你 W3 要修的东西。

**操作**：把两个通道的 `base_url` **都**改成 `http://127.0.0.1:9/v1`，重启，发一次请求：

```powershell
$body = @{ model = "auto"; messages = @(@{ role = "user"; content = "你好" }) } | ConvertTo-Json -Depth 5
$r = Invoke-WebRequest -Uri "http://127.0.0.1:8080/v1/chat/completions" -Method Post -Body $body -ContentType "application/json"
"HTTP 状态码: $($r.StatusCode)"
$r.Content
```

**观察**：**HTTP 200，但内容是一段错误文本**，`x_gateway.attempted_channels` 里两个通道都试过了。

**要回答的问题**：
1. 为什么业务侧拿到 200 不能认为成功？真实生产里这会造成什么事故？（提示：重试逻辑、计费、监控告警全部失效）
2. 打开 `src/api/completions.py` 第 110–154 行，指出是哪几行导致了这个行为？
3. 你会怎么改？如果改成 502，`src/gateway/router.py` 里 `success=False` 但 `response` 有值的那条路径要怎么处理？
4. 改完之后，`tests/test_gateway.py` 里的 `test_chat_completion` 还会 PASS 吗？（它现在判断的是 `status_code == 200` 或 `502`）

**通过标准**：能说清"上游错误必须映射成 HTTP 错误码 + 结构化错误体"，并给出改动方案。

---

## 做完之后

把 5 个实验的答案 + 15 行笔记写进 `docs/NOTES_W1.md`，然后来找我。我会拿其中的问题**当场追问两层**（原理 → 那如果……呢），过得了就算 W1 通过，进 W2 亲手重写核心 120 行。

**还原现场**：
```powershell
Copy-Item config\providers.yaml.bak config\providers.yaml -Force
Copy-Item data\gateway.db.bak data\gateway.db -Force
```

---

## 为什么这条路比"背题"快

因为你需要的是**在面试里被追问时还能答上来**的能力，不是标准答案。追问一定落在"如果换成另一种情况呢"——只有在你自己把它搞坏过、看过它怎么恢复的人，才答得出来。这 5 个实验就是低成本地把每一种"如果"都试一遍。

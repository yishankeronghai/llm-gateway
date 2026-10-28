# 大模型网关开源项目分析报告

> 分析 GitHub 上大模型网关、智能负载均衡、调度、提示词管理、命令审计相关项目

---

## 一、项目筛选结果

从 GitHub 搜索到的相关项目中，筛选出以下 5 个最具代表性的项目：

| 项目 | 语言 | Stars | 核心特点 |
|------|------|-------|----------|
| **LLMGov** | Python/FastAPI | — | 断路器状态机、防篡改审计哈希链、PII 脱敏 |
| **AIGC Gateway** | TypeScript/Next.js | — | 10+ 服务商路由、提示词工作流(Action/Template)、计费对账 |
| **Helicone AI Gateway** | Rust | 608 | <5ms 延迟、P2C 负载均衡、Redis/S3 缓存 |
| **PromptShield Gateway** | Go | — | 限流、审计日志、Token 追踪、Prometheus 指标 |
| **Portkey AI Gateway** | TypeScript | 8k+ | 1600+ 模型、Guardrails、语义缓存、50+ 监控指标 |

---

## 二、重点模块分析

### 2.1 网关路由 (Gateway Routing)

**实现方法对比：**

| 项目 | 路由方式 | 关键设计 |
|------|----------|----------|
| LLMGov | LiteLLM 抽象层 → 单提供商 + 本地回退 | 断路器触发后自动切换到 Ollama 本地模型 |
| AIGC Gateway | resolveEngine(model) → alias → channel | 优先级排序 + 健康状态过滤 |
| Helicone | 插拔式策略 (latency-based P2C) | Power of Two Choices 算法 |
| Portkey | 自动 fallback + load balancing + canary | 基于 latency/cost/context 动态选择 |

**关键设计模式：**
- **适配器模式**：所有提供商统一为 OpenAI 兼容接口
- **别名映射**：客户端使用简短别名，网关解析为实际模型
- **优先级队列**：通道按优先级排序，高优先级优先调度

### 2.2 智能负载均衡 (Load Balancing)

**策略对比：**

| 策略 | 实现复杂度 | 适用场景 |
|------|-----------|----------|
| 加权轮询 (Weighted Round Robin) | 低 | 通道能力有差异 |
| 最低延迟 (Least Latency) | 中 | 对延迟敏感 |
| P2C (Power of Two Choices) | 中 | 大规模多通道 |
| 随机 (Random) | 最低 | 简单场景 |

**Helicone P2C 算法核心思想：**
1. 随机选取 2 个候选通道
2. 比较两者的延迟/负载
3. 选择更优的一个
4. 避免了全局排序的开销，同时保证质量

### 2.3 断路器与故障转移 (Circuit Breaker & Failover)

**LLMGov 的断路器状态机：**

```
状态转换：
  CLOSED → OPEN:     连续失败 ≥ failure_threshold
  OPEN → HALF_OPEN:  经过 recovery_timeout 冷却
  HALF_OPEN → CLOSED: 探测请求成功
  HALF_OPEN → OPEN:  探测请求失败

关键设计：
  - HALF_OPEN 并发安全: asyncio 竞争条件检查，确保只有一个探测请求在飞行中
  - 混沌测试: 自动化状态机混沌测试系统性触发故障
  - 即时低延迟回退: 主提供商故障时自动路由到本地 Ollama
```

### 2.4 提示词管理 (Prompt Management)

**AIGC Gateway 的两层抽象：**

| 层级 | 概念 | 核心特性 |
|------|------|----------|
| **Action** | 单次调用的可复用 prompt | 版本管理、变量注入({{var}})、Dry-run 预览 |
| **Template** | 多步编排工作流 | 串行(Sequential)、并行分拆(Fan-out)、版本固定(lockedVersionId) |

**版本管理与 A/B 测试：**
- 每次修改创建新版本，`activeVersionId` 决定当前活跃版本
- 加权 A/B 测试：按权重随机选择版本
- 版本固定：生产运行不受上游编辑影响

### 2.5 命令审计 (Command Audit)

**LLMGov 的防篡改审计哈希链：**

```
设计原理：
  每条审计记录包含:
  - prev_hash: 前一条记录的 curr_hash
  - curr_hash: SHA256(prev_hash + canonical_json(data))
  
  形成链式结构:
  Record1 → Record2 → Record3 → ...
  
  任何篡改 → curr_hash 变化 → 后续 prev_hash 不匹配 → 检测到篡改
```

**数据存储：**
- LLMGov: ClickHouse 列式数据库，异步写入不阻塞热路径
- AIGC Gateway: PostgreSQL + FOR UPDATE 行级锁
- PromptShield: Prometheus 指标 + 审计日志

---

## 三、技术栈对比

| 维度 | LLMGov | AIGC Gateway | Helicone | PromptShield |
|------|--------|-------------|----------|-------------|
| **语言** | Python | TypeScript | Rust | Go |
| **框架** | FastAPI | Next.js 14 | Axum | net/http |
| **数据库** | ClickHouse | PostgreSQL | Redis/S3 | Prometheus |
| **缓存** | Redis (语义缓存) | Redis | Redis/S3 | — |
| **部署** | Docker + Terraform | Docker + GCP | Docker | Docker |
| **监控** | Grafana | 内置 Dashboard | OpenTelemetry | Prometheus |

---

## 四、本地实现方案

基于上述分析，本项目提取了以下核心模块进行本地实现：

| 模块 | 参考来源 | 实现方式 |
|------|----------|----------|
| 模型路由器 | AIGC Gateway resolveEngine | alias → channel(优先级排序) → LB 选择 |
| 负载均衡器 | Helicone P2C + AIGC 优先级 | 加权轮询 + 最低延迟 + 轮询 + 随机 |
| 断路器 | LLMGov circuit_breaker.py | CLOSED→OPEN→HALF_OPEN 状态机 |
| 提示词注册表 | LLMGov prompt_registry + AIGC Action | YAML 版本化 + 加权 A/B + 变量渲染 |
| 审计哈希链 | LLMGov Phase 2 audit hash-chain | SHA-256 链 + SQLite 存储 |
| 限流器 | LLMGov rate_limiter (fail-open) | 滑动窗口 + 故障开启 |
| 响应缓存 | LLMGov cache.py | SHA-256 请求哈希 + TTL |
| Web 仪表盘 | AIGC 管理控制台 | 原生 HTML/CSS/JS |

---

## 五、各项目 GitHub 链接

- LLMGov: https://github.com/LeviAckermanZ9/LLMGov
- AIGC Gateway: https://github.com/tripplemay/aigcgateway
- Helicone AI Gateway: https://github.com/Helicone/ai-gateway
- PromptShield Gateway: https://github.com/promptshieldhq/promptshield-gateway
- Portkey AI Gateway: https://github.com/Portkey-AI/gateway
- sxueck/llm-gateway: https://github.com/sxueck/llm-gateway
- LLM-MCP: https://github.com/NolanRobbins/LLM-MCP
- PromptSail: https://github.com/PromptSail/prompt_sail

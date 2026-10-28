"""
实验 1: priority 管分组, weight 管组内分流
==========================================
用法: python experiments/exp1_priority.py <config_dir> [请求次数]

对应 docs/STUDY_PLAN.md 第 1 周实验 1。
用 mock 上游 (不消耗 API 额度) 连续发 N 次 model=auto 请求,
统计流量落在哪个通道上, 直观看到 priority 与 weight 的分工:

  base     DeepSeek priority=1, OpenAI priority=2   → 预期 100% 走 DeepSeek (主备语义)
  v_eq     两个都 priority=1                        → 预期约 50/50 (weight 100:100 生效)
  v_weight 两个都 priority=1, OpenAI weight=50      → 预期约 67/33 (weight 100:50 生效)

核心机制: load_balancer.select() 先按 priority 分组只留最低组,
         再在组内按策略 (weighted_round_robin) 分配流量。
"""

import asyncio
import collections
import sys
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.config import config
from src.models.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Message,
    Usage,
)
from src.gateway.router import router
from src.gateway.provider import provider_manager
from src.gateway.circuit_breaker import breaker_manager
from src.gateway.load_balancer import load_balancer


def make_response(model: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="mock",
        created=0,
        model=model,
        choices=[Choice(index=0, message=Message(role="assistant", content="ok"), finish_reason="stop")],
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def setup_mocks():
    """两个上游都 mock 成永远成功, 这样观察到的差异只来自负载均衡决策"""
    for name in ("deepseek", "openai"):
        adapter = AsyncMock()
        adapter.chat_completion.return_value = (make_response(f"mock-{name}"), 50.0, True)
        provider_manager._adapters[name] = adapter


async def main():
    cfg_dir = sys.argv[1] if len(sys.argv) > 1 else None
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    config.load(config_dir=cfg_dir)

    print(f"  配置: {cfg_dir or '(默认)'}")
    print("  通道优先级/权重:")
    for p in config.providers:
        for ch in p.channels:
            print(f"    {ch.id:18s} priority={ch.priority}  weight={ch.weight}")

    setup_mocks()
    breaker_manager._breakers.clear()
    load_balancer._weights.clear()  # 清掉加权列表缓存, 避免脏数据

    counts = collections.Counter()
    for _ in range(n):
        req = ChatCompletionRequest(model="auto", messages=[{"role": "user", "content": "hi"}])
        result = await router.route(req)
        counts[result.channel_id or "(未路由)"] += 1

    print(f"\n  连续 {n} 次 model=auto 请求的流量分布:")
    for channel_id, c in counts.most_common():
        pct = c / n * 100
        bar = "#" * round(pct / 4)
        print(f"    {channel_id:18s} {c:3d} 次  {pct:5.1f}%  {bar}")
    missing = [c for c in ("deepseek-primary", "openai-primary") if c not in counts]
    for m in missing:
        print(f"    {m:18s}   0 次    0.0%  ← 一次都没被选中")

    # 揭示加权列表的内部结构 (weighted_round_robin 的真实机制) + 期望值对照
    for key, pool in load_balancer._weights.items():
        names = collections.Counter(x["channel_id"] for x in pool)
        total = sum(names.values())
        print(f"\n  加权列表(pool)内部结构: {dict(names)}  ← weight//10 展开后的抽签池")
        print(f"  期望值 vs 实测 (样本量 n={n}):")
        for cid in sorted({c["channel_id"] for c in pool}):
            exp_pct = names.get(cid, 0) / total * 100
            obs_pct = counts.get(cid, 0) / n * 100
            print(f"    {cid:18s} 期望 {exp_pct:5.1f}%   实测 {obs_pct:5.1f}%   "
                  f"偏差 {obs_pct - exp_pct:+5.1f} pp")


asyncio.run(main())

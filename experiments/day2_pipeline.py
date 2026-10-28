"""
Day 2 演示: 请求管道 9 步全流程实测
====================================
用法: python experiments/day2_pipeline.py

用 httpx.ASGITransport 在进程内直接调用 FastAPI app, 走完整管道:
  Auth → RateLimit → 解析body → Prompt → Cache → Router → CacheSet → Audit → 返回
上游用 mock 替换, 因此不需要 API Key。

验证 8 个场景, 并顺带抓出管道实现里的三处"不一致"。
"""

import os
import sys

# ⚠️ 必须在 import src.* 之前设置: auth_manager 在模块导入时就读取环境变量
os.environ["GATEWAY_API_KEYS"] = "gw-demo-key-123,gw-demo-key-456"

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import httpx

from src.main import app
from src.config import config
from src.gateway.provider import provider_manager
from src.prompt.registry import prompt_registry
from src.audit.logger import audit_logger
from src.models.schemas import ChatCompletionResponse, Choice, Message, Usage

KEY_A = {"Authorization": "Bearer gw-demo-key-123"}
KEY_B = {"Authorization": "Bearer gw-demo-key-456"}


def make_response(model: str, content: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="mock-1",
        created=0,
        model=model,
        choices=[Choice(index=0, message=Message(role="assistant", content=content), finish_reason="stop")],
        usage=Usage(prompt_tokens=12, completion_tokens=34, total_tokens=46),
    )


def setup_mocks(ok: bool = True):
    for name in ("deepseek", "openai"):
        adapter = AsyncMock()
        adapter.chat_completion.return_value = (
            make_response(f"mock-{name}", f"[{name}] reply OK" if ok else f"[{name}] HTTP 503 upstream error"),
            88.0,
            ok,
        )
        provider_manager._adapters[name] = adapter


def show(title: str):
    print(f"\n{'=' * 72}\n  {title}\n{'=' * 72}")


async def main():
    # ── 手动执行 lifespan 的启动逻辑 (ASGITransport 不跑 lifespan) ──
    config.load()
    provider_manager.init()          # 建真实 adapter
    prompt_registry.load()
    audit_logger.init()              # 启动异步写入协程
    setup_mocks(ok=True)             # 再用 mock 覆盖 adapter

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
        body = {"model": "auto", "messages": [{"role": "user", "content": "你好, 请自我介绍"}]}

        # ── 场景 1: 认证 (管道第 1 步) ──
        show("场景 1 · 认证 fail-close (管道第 1 步)")
        r = await c.post("/v1/chat/completions", json=body)
        print(f"  无 Authorization 头        → HTTP {r.status_code}  {r.json().get('detail')}")
        r = await c.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer wrong-key"})
        print(f"  错误的 API Key            → HTTP {r.status_code}  {r.json().get('detail')}")
        print("  结论: 认证在所有步骤之前, 未认证请求不消耗任何后续资源")

        # ── 场景 2: 正常请求 (完整管道) ──
        show("场景 2 · 正常请求 → 完整管道 + x_gateway 元信息")
        r = await c.post("/v1/chat/completions", json=body, headers=KEY_A)
        data = r.json()
        print(f"  HTTP {r.status_code} | 回复: {data['choices'][0]['message']['content']}")
        print(f"  usage: {data['usage']}")
        print(f"  x_gateway: {json.dumps(data.get('x_gateway'), ensure_ascii=False, indent=2)}")

        # ── 场景 3: 缓存命中 (管道第 5 步) ──
        show("场景 3 · 相同请求第二次 → 缓存命中 (管道第 5 步)")
        r2 = await c.post("/v1/chat/completions", json=body, headers=KEY_A)
        d2 = r2.json()
        print(f"  HTTP {r2.status_code} | 回复: {d2['choices'][0]['message']['content']}")
        print(f"  第一次响应含 x_gateway: {'x_gateway' in data}")
        print(f"  第二次响应含 x_gateway: {'x_gateway' in d2}   ← ⚠️ 不一致!")
        print(f"  缓存命中时客户端无法得知'这是缓存', 也拿不到 trace_id")
        print(f"  缓存统计: {json.dumps(response_cache_stats(), ensure_ascii=False)}")
        print("  注意: 缓存命中依然占用限流额度 (限流在第 2 步, 缓存检查在第 5 步)")

        # ── 场景 4: 请求体解析 (管道第 3 步) ──
        show("场景 4 · 非法请求体 (管道第 3 步)")
        r = await c.post("/v1/chat/completions", json={"model": "auto"}, headers=KEY_A)
        print(f"  缺少 messages 字段 → HTTP {r.status_code}")
        print(f"  detail: {r.json().get('detail')}")

        # ── 场景 5: 上游失败时 HTTP 码与审计码不一致 ──
        show("场景 5 · 上游全部失败 → HTTP 码与审计记录不一致")
        setup_mocks(ok=False)          # 让两个上游都失败
        r = await c.post("/v1/chat/completions",
                         json={"model": "auto", "messages": [{"role": "user", "content": "触发失败-1"}]},
                         headers=KEY_A)
        d = r.json()
        print(f"  客户端看到的 HTTP 状态码: {r.status_code}")
        print(f"  响应内容: {d['choices'][0]['message']['content'][:60]}")
        print(f"  x_gateway.provider: {d.get('x_gateway', {}).get('provider')}")
        print(f"  x_gateway.attempted: {d.get('x_gateway', {}).get('attempted_channels')}")
        print("  ⚠️ 上游失败被包装成 HTTP 200 (错误信息藏在 message.content 里)")

        # ── 场景 6: 断路器熔断 → 真正的 502 ──
        show("场景 6 · 连续失败熔断所有通道 → 返回真 502")
        for i in range(2, 6):
            await c.post("/v1/chat/completions",
                         json={"model": "auto", "messages": [{"role": "user", "content": f"触发失败-{i}"}]},
                         headers=KEY_A)
        from src.gateway.circuit_breaker import breaker_manager
        states = {k: v.state.value for k, v in breaker_manager._breakers.items()}
        print(f"  5 次失败后断路器状态: {states}")
        r = await c.post("/v1/chat/completions",
                         json={"model": "auto", "messages": [{"role": "user", "content": "熔断后请求"}]},
                         headers=KEY_A)
        print(f"  熔断后再请求 → HTTP {r.status_code}  {r.json().get('detail')}")
        print("  结论: 只有'所有断路器都打开'时, response 为 None, 才会走到 502 分支")

        # ── 场景 7: 限流 (管道第 2 步, fail-open) ──
        show("场景 7 · 滑动窗口限流 (管道第 2 步)")
        config.rate_limiting.default_rpm = 3     # 为演示方便调小阈值
        setup_mocks(ok=True)
        print("  (为便于观察, 把 rpm 临时调成 3, 并使用一个全新的 API Key)")
        for i in range(1, 6):
            r = await c.post("/v1/chat/completions",
                             json={"model": "auto", "messages": [{"role": "user", "content": "限流测试"}]},
                             headers=KEY_B)
            retry = r.headers.get("Retry-After")
            extra = f"  Retry-After={retry}s" if retry else ""
            detail = r.json().get("detail") if r.status_code != 200 else "通过"
            print(f"  第 {i} 次请求 → HTTP {r.status_code}  {detail}{extra}")
        print("  结论: 认证之后限流之前拒绝, 且按 API Key 维度独立计数")

        # ── 场景 8: 审计 (管道第 8 步) ──
        show("场景 8 · 审计日志与哈希链 (管道第 8 步)")
        await asyncio.sleep(0.6)                 # 等异步写入协程把队列写进 SQLite
        r = await c.get("/v1/audit/logs?limit=6")
        logs = r.json()
        print(f"  最近 {logs['count']} 条审计记录 (注意 api_key 已脱敏、status_code 字段):")
        for entry in logs["logs"]:
            print(f"    trace={entry['trace_id']} | provider={entry['provider']:9s} | "
                  f"channel={entry['channel_id']:14s} | status={entry['status_code']} | "
                  f"key={entry['api_key']} | latency={entry['latency_ms']}ms")
        r = await c.get("/v1/audit/verify")
        v = r.json()
        print(f"\n  哈希链验证: {'✅ ' if v['valid'] else '❌ '}{v['message']} (共 {v['total']} 条)")
        print("  ⚠️ 注意上面失败请求的 status=500, 而客户端实际收到的是 HTTP 200")


def response_cache_stats() -> dict:
    from src.cache.response_cache import response_cache
    return response_cache.stats()


asyncio.run(main())

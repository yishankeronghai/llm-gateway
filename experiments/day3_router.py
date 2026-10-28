"""
Day 3 演示: router.py 与 provider.py 深度实测
==============================================
用法: python experiments/day3_router.py

覆盖 7 个观察点:
  1. 故障转移循环的精确行为 (attempted 序列 / 真正被调用的通道)
  2. 断路器打开时的跳过路径 (以及 attempted_channels 的语义问题)
  3. 未知模型 → 无候选通道时的优雅降级
  4. 上游返回空 choices → router 是否会崩
  5. 适配器实际透传给上游的字段 (payload 实测)
  6. "通道=endpoint" 这个说法到底实现了没有
  7. httpx 超时异常能否被 except asyncio.TimeoutError 捕获
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import httpx

from src.config import config, ProviderConfig, ChannelConfig
from src.models.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Message,
    Usage,
)
from src.gateway.router import router
from src.gateway.provider import provider_manager, ProviderAdapter
from src.gateway.circuit_breaker import breaker_manager


def make_response(model: str, content: str, success_content: str = None, choices: bool = True) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="mock",
        created=0,
        model=model,
        choices=(
            [Choice(index=0, message=Message(role="assistant", content=success_content or content), finish_reason="stop")]
            if choices else []
        ),
        usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
    )


def setup_mocks(deepseek_ok: bool = True, openai_ok: bool = True):
    ds = AsyncMock()
    ds.chat_completion.return_value = (make_response("ds", "ds", "[deepseek] ok" if deepseek_ok else "[deepseek] HTTP 503"), 100.0, deepseek_ok)
    oa = AsyncMock()
    oa.chat_completion.return_value = (make_response("oa", "oa", "[openai] ok" if openai_ok else "[openai] HTTP 503"), 200.0, openai_ok)
    provider_manager._adapters["deepseek"] = ds
    provider_manager._adapters["openai"] = oa
    return ds, oa


def show(t):
    print(f"\n{'=' * 72}\n  {t}\n{'=' * 72}")


def req(model="auto", content="hi"):
    return ChatCompletionRequest(model=model, messages=[{"role": "user", "content": content}])


async def main():
    config.load()
    provider_manager.init()

    # ── 1. 故障转移循环 ──
    show("观察点 1 · 故障转移循环的精确行为")
    breaker_manager._breakers.clear()
    ds, oa = setup_mocks(deepseek_ok=True)
    r = await router.route(req())
    print(f"  两个上游都正常 → provider={r.provider} | attempted={r.attempted_channels}")
    print(f"    DeepSeek 被调用次数 = {ds.chat_completion.call_count}, OpenAI 被调用次数 = {oa.chat_completion.call_count}")

    breaker_manager._breakers.clear()
    ds, oa = setup_mocks(deepseek_ok=False)
    r = await router.route(req())
    print(f"\n  DeepSeek 失败 → provider={r.provider} | attempted={r.attempted_channels} | success={r.success}")
    print(f"    DeepSeek 被调用次数 = {ds.chat_completion.call_count}, OpenAI 被调用次数 = {oa.chat_completion.call_count}")
    print(f"    返回给客户端的是哪家的响应? message = {r.response.choices[0].message.content!r}")
    print("    ↑ 注意: result.response 被每次尝试覆盖, 最终返回的是【最后一次尝试】的响应")

    # ── 2. 断路器跳过路径 ──
    show("观察点 2 · 断路器打开时的跳过路径")
    breaker_manager._breakers.clear()
    cb = breaker_manager.get("deepseek-primary")
    for _ in range(5):
        await cb.record_failure()
    print(f"  手动把 deepseek 断路器打成: {cb.state.value}")
    ds, oa = setup_mocks(deepseek_ok=True)
    r = await router.route(req())
    print(f"  provider={r.provider} | attempted={r.attempted_channels}")
    print(f"  DeepSeek 被调用次数 = {ds.chat_completion.call_count}   ← 断路器拦住了, 根本没调用上游")
    print(f"  → load_balancer.select() 在过滤阶段就排除了 open 的通道 (load_balancer.py L100),")
    print(f"    所以这条路径上 router.py L92 的断路器检查不会触发, attempted_channels 是准的")
    print(f"  → router.py L92 真正生效的场景是 HALF_OPEN 探测名额被占满时:")
    print(f"    select() 的 half_open 兜底会把半开通道选出来, 再由 L92 的 can_execute() 拦回去,")
    print(f"    此时该通道会被 append 进 attempted_channels 但从未被调用 (语义不准)")

    # ── 3. 未知模型 → 优雅降级 ──
    show("观察点 3 · 未知模型 → 无候选通道时的处理")
    breaker_manager._breakers.clear()
    r = await router.route(req(model="不存在的模型"))
    print(f"  success={r.success} | provider={r.provider!r} | error={r.error}")
    print(f"  response 非空: {r.response is not None} (由 _no_provider_response 合成)")
    print(f"  返回内容: {r.response.choices[0].message.content[:70]}")
    print("  ✅ 优点: 优雅降级(返回可读提示), 而不是直接抛异常")

    # ── 4. 上游返回空 choices ──
    show("观察点 4 · 上游返回空 choices 会怎样 (健壮性)")
    breaker_manager._breakers.clear()
    ds, oa = setup_mocks()
    ds.chat_completion.return_value = (make_response("ds", "ds", choices=False), 100.0, False)
    oa.chat_completion.return_value = (make_response("oa", "oa", choices=False), 200.0, False)
    try:
        r = await router.route(req(content="空choices测试"))
        print(f"  未崩溃, provider={r.provider}, error={r.error}")
    except Exception as e:
        print(f"  ❌ 崩溃: {type(e).__name__}: {e}")
        print("     原因: router.py L137 直接访问 response.choices[0],")
        print("     而适配器允许 choices 为空列表 (schemas.py 的 list[Choice] 没有最小长度限制)")
        print("     上游做内容过滤时可能返回空 choices → 网关 500")

    # ── 5. 适配器透传字段实测 ──
    show("观察点 5 · 适配器实际发给上游的字段 (payload 实测)")
    pcfg = ProviderConfig(name="demo-upstream", base_url="https://upstream.example/v1",
                          api_key="test-key", timeout=30,
                          channels=[ChannelConfig(id="demo-ch", models=["m"])])
    adapter = ProviderAdapter(pcfg)
    await adapter.close()                     # 关掉真实 client, 换成 mock
    adapter.client = AsyncMock()
    adapter.client.post.return_value = httpx.Response(200, json={
        "id": "x", "created": 1, "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })

    client_body = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.7,
        "max_tokens": 128,
        "top_p": 0.9,
        # 以下都是 OpenAI 标准参数, 观察网关怎么处理
        "stream": True,
        "stop": ["\n"],
        "n": 2,
        "presence_penalty": 0.5,
        "response_format": {"type": "json_object"},
    }
    parsed = ChatCompletionRequest(**client_body)
    print(f"  客户端提交的 9 个字段")
    print(f"  Pydantic 解析后 gateway 认识的字段: {list(parsed.model_dump().keys())}")
    print(f"    ⚠️ stop/n/presence_penalty/response_format 在解析阶段就被静默丢弃 (extra='ignore')")
    await adapter.chat_completion(pcfg.channels[0], parsed)
    sent = adapter.client.post.call_args.kwargs.get("json") or adapter.client.post.call_args[1]["json"]
    print(f"\n  适配器实际发出的 payload 字段: {sorted(sent.keys())}")
    print(f"    stream 在 payload 里吗? {'stream' in sent}   ← 客户端要流式, 网关不会告诉你做不到")
    print(f"  payload 全文: {sent}")

    # ── 6. 通道 = endpoint 吗? ──
    show("观察点 6 · \"每个通道是一个 endpoint\" 这个声明实现了吗")
    print(f"  ChannelConfig 支持的字段: {list(ChannelConfig.model_fields.keys())}")
    print(f"    → 没有 base_url / timeout / api_key 字段")
    adapter2 = ProviderAdapter(config.providers[0])
    print(f"  ProviderAdapter 绑定的 base_url: {adapter2.client.base_url}  ← 来自 provider, 不是 channel")
    await adapter2.close()
    print(f"  provider_manager._adapters 的键: {list(provider_manager._adapters.keys())}")
    print(f"    → 每个 provider 只有一个 client, 该 provider 下所有通道共享同一个 base_url")
    print("  ⚠️ 后果: 同一 provider 下配多个通道, 实际打的是同一个端点,")
    print("     '在通道间负载均衡' 没有意义; 想接两个不同 endpoint 只能声明成两个 provider")
    print("     (而 providers.yaml 注释和 README 都说'通道=endpoint')")

    # ── 7. httpx 超时异常能否被捕获 ──
    show("观察点 7 · except asyncio.TimeoutError 能捕获 httpx 的超时吗")
    print(f"  httpx.TimeoutException 的继承链: {[c.__name__ for c in httpx.TimeoutException.__mro__]}")
    print(f"  issubclass(httpx.TimeoutException, asyncio.TimeoutError) = "
          f"{issubclass(httpx.TimeoutException, asyncio.TimeoutError)}")
    print(f"  issubclass(httpx.ReadTimeout, asyncio.TimeoutError) = "
          f"{issubclass(httpx.ReadTimeout, asyncio.TimeoutError)}")

    adapter3 = ProviderAdapter(pcfg)
    await adapter3.close()
    adapter3.client = AsyncMock()
    adapter3.client.post.side_effect = httpx.ReadTimeout("simulated read timeout")
    resp, lat, ok = await adapter3.chat_completion(pcfg.channels[0], parsed)
    print(f"\n  真实 httpx 读超时 → success={ok}, 返回内容: {resp.choices[0].message.content!r}")
    if "Request timeout after" in resp.choices[0].message.content:
        print("     → 命中了 except asyncio.TimeoutError 分支")
    else:
        print("     → 落到了 except Exception 通用分支 (timeout 专用分支没生效)")

    adapter3.client.post.side_effect = asyncio.TimeoutError()
    resp, lat, ok = await adapter3.chat_completion(pcfg.channels[0], parsed)
    print(f"  asyncio.TimeoutError → success={ok}, 返回内容: {resp.choices[0].message.content!r}")


asyncio.run(main())

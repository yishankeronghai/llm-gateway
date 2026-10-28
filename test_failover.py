"""
故障转移验证脚本 - 验证 DeepSeek 挂了自动切 OpenAI
================================================
运行: python test_failover.py
不依赖真实 API Key, 用 mock 模拟上游行为:
  场景1: DeepSeek 正常   → 请求走 DeepSeek (模型翻译为 deepseek-chat)
  场景2: DeepSeek 故障   → 自动切到 OpenAI (模型翻译为 gpt-4o-mini)
  场景3: DeepSeek 连续失败5次 → 断路器熔断, 后续请求直接走 OpenAI
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent))

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


def make_response(model: str, content: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="mock-1",
        created=0,
        model=model,
        choices=[Choice(index=0, message=Message(role="assistant", content=content), finish_reason="stop")],
        usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
    )


def setup_mock_adapters(deepseek_ok: bool):
    """用 mock 替换真实 adapter, 控制 DeepSeek 成功/失败"""
    ds = AsyncMock()
    if deepseek_ok:
        ds.chat_completion.return_value = (make_response("deepseek-chat", "DeepSeek reply OK"), 100.0, True)
    else:
        ds.chat_completion.return_value = (
            make_response("deepseek-chat", "HTTP 503 service unavailable"), 100.0, False,
        )
    oa = AsyncMock()
    oa.chat_completion.return_value = (make_response("gpt-4o-mini", "OpenAI fallback reply OK"), 200.0, True)
    provider_manager._adapters["deepseek"] = ds
    provider_manager._adapters["openai"] = oa
    return ds, oa


def make_request(model: str = "auto"):
    return ChatCompletionRequest(model=model, messages=[{"role": "user", "content": "hello"}])


async def run_scenario(name: str, deepseek_ok: bool, expect_provider: str):
    print(f"\n{'=' * 64}")
    print(f"场景: {name}")
    print(f"{'=' * 64}")

    breaker_manager._breakers.clear()  # 重置断路器, 避免场景间相互影响
    ds, oa = setup_mock_adapters(deepseek_ok)

    result = await router.route(make_request())
    print(f"  model_used   : {result.model_used}")
    print(f"  provider     : {result.provider}")
    print(f"  channel      : {result.channel_id}")
    print(f"  attempted    : {result.attempted_channels}")
    print(f"  success      : {result.success}")
    if result.response and result.response.choices:
        print(f"  reply        : {result.response.choices[0].message.content[:40]}")

    if ds.chat_completion.called:
        print(f"  发给 DeepSeek : model={ds.chat_completion.call_args[0][1].model}")
    if oa.chat_completion.called:
        print(f"  发给 OpenAI   : model={oa.chat_completion.call_args[0][1].model}")

    ok = result.provider == expect_provider and result.success
    print(f"  >>> {'PASS' if ok else 'FAIL'}")
    return ok


async def scenario_3_breaker():
    """场景3: DeepSeek 连续失败 5 次 → 断路器 OPEN → 之后直接走 OpenAI"""
    print(f"\n{'=' * 64}")
    print("场景: DeepSeek 连续失败5次 → 断路器熔断 → 直接走 OpenAI")
    print(f"{'=' * 64}")

    breaker_manager._breakers.clear()
    ds, oa = setup_mock_adapters(False)

    for i in range(1, 6):
        r = await router.route(make_request())
        print(f"  第{i}次: provider={r.provider:8s} | success={r.success} "
              f"| breaker={breaker_manager.get('deepseek-primary').state.value}")

    # 第 5 次失败后断路器应 OPEN
    cb = breaker_manager.get("deepseek-primary")
    print(f"  >>> DeepSeek 断路器状态: {cb.state.value}")
    assert cb.state.value == "open", "DeepSeek 断路器应为 open"

    # 再发一次请求, 应直接走 OpenAI (DeepSeek 被熔断过滤)
    ds, oa = setup_mock_adapters(True)  # 即便 DeepSeek 恢复了, 熔断期内也不该选它
    r = await router.route(make_request())
    print(f"  熔断后请求: provider={r.provider} | attempted={r.attempted_channels}")
    print(f"  DeepSeek 被调用: {ds.chat_completion.called}")
    ok = r.provider == "openai" and not ds.chat_completion.called
    print(f"  >>> {'PASS' if ok else 'FAIL'}")
    return ok


async def main():
    config.load()  # 幂等

    # 预检: 配置解析正确性
    print("=" * 64)
    print("预检: find_channels_for_model('auto') 候选通道")
    print("=" * 64)
    chans = config.find_channels_for_model("auto")
    for p, ch in chans:
        print(f"  {p.name:10s} | {ch.id:20s} | priority={ch.priority} | "
              f"model_map={ch.model_map}")
    assert len(chans) == 2, "应有 2 个候选通道 (deepseek + openai)"
    assert chans[0][1].priority == 1, "DeepSeek 应优先"
    assert chans[0][1].model_map.get("auto-chat") == "deepseek-chat"
    assert chans[1][1].model_map.get("auto-chat") == "gpt-4o-mini"
    print("  >>> PASS\n")

    results = []
    results.append(await run_scenario("DeepSeek 正常 → 走 DeepSeek (主)", True, "deepseek"))
    results.append(await run_scenario("DeepSeek 故障 → 自动切 OpenAI (备)", False, "openai"))
    results.append(await scenario_3_breaker())

    print("\n" + "=" * 64)
    print(f"  最终结果: {sum(results)}/{len(results)} 通过")
    print("=" * 64)
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())

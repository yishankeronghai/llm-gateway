"""
实验 2: model_map 如何决定「路由资格」与「上游真实模型名」
=========================================================
用法: python experiments/exp2_model_map.py <config_dir>

配合 docs/STUDY_PLAN.md 第 1 周的动手实验。
用 AsyncMock 替换真实 adapter (不消耗 API 额度), 观察三个问题:

  1. find_channels_for_model('auto') 返回几个候选通道? 各自的 model_map 是什么?
  2. DeepSeek 正常时, 实际发给上游的 model 名是什么?
  3. DeepSeek 故障时能否切到 OpenAI? 切过去后发给 OpenAI 的 model 名是什么?

关键结论: model_map 有双重身份 ——
  (a) 路由资格: 虚拟模型名登记在 model_map 里, 该通道才会被选为候选
  (b) 名称翻译: 命中候选后, 按该通道自己的 model_map 翻译成真实模型名
"""

import asyncio
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


def make_response(model: str, content: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="mock-1",
        created=0,
        model=model,
        choices=[Choice(index=0, message=Message(role="assistant", content=content), finish_reason="stop")],
        usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
    )


def setup_mocks(deepseek_ok: bool):
    """mock 掉两个上游 adapter; deepseek_ok 控制 DeepSeek 成功/失败"""
    ds = AsyncMock()
    ds.chat_completion.return_value = (
        make_response("mock-deepseek", "DeepSeek reply OK" if deepseek_ok else "HTTP 503 service unavailable"),
        100.0,
        deepseek_ok,
    )
    oa = AsyncMock()
    oa.chat_completion.return_value = (
        make_response("mock-openai", "OpenAI fallback reply OK"),
        200.0,
        True,
    )
    provider_manager._adapters["deepseek"] = ds
    provider_manager._adapters["openai"] = oa
    return ds, oa


async def main():
    cfg_dir = sys.argv[1] if len(sys.argv) > 1 else None
    config.load(config_dir=cfg_dir)

    print(f"  配置来源: {cfg_dir or '(默认 config/)'}")

    print("\n  [1] 候选通道 find_channels_for_model('auto'):")
    chans = config.find_channels_for_model("auto")
    if not chans:
        print("      (无候选通道 → 请求会直接返回错误提示)")
    for p, ch in chans:
        print(f"      {p.name:9s} | {ch.id:16s} | priority={ch.priority} | model_map={ch.model_map}")
    print(f"      → 候选数量 = {len(chans)} (这决定了故障转移最多能试几次)")

    for label, ds_ok in [("DeepSeek 正常", True), ("DeepSeek 故障", False)]:
        breaker_manager._breakers.clear()
        ds, oa = setup_mocks(ds_ok)
        req = ChatCompletionRequest(model="auto", messages=[{"role": "user", "content": "hello"}])
        result = await router.route(req)

        print(f"\n  [{'2' if ds_ok else '3'}] {label}:")
        print(f"      provider={result.provider} | channel={result.channel_id} | success={result.success}")
        print(f"      attempted={result.attempted_channels}")
        if ds.chat_completion.called:
            print(f"      >>> 发给 DeepSeek 的真实 model = {ds.chat_completion.call_args[0][1].model}")
        if oa.chat_completion.called:
            print(f"      >>> 发给 OpenAI   的真实 model = {oa.chat_completion.call_args[0][1].model}")
        if not result.success:
            print(f"      故障转移结果: 全部候选通道尝试完毕, 请求失败")


asyncio.run(main())

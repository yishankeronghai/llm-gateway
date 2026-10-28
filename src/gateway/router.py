"""
模型路由器 (Model Router)
负责: 模型别名解析 → 候选通道查找 → 负载均衡选择 → 提供商调用 → 断路器记录

这是网关的核心调度模块，将请求路由到最合适的上游通道

参考: AIGC Gateway 的 resolveEngine(model) → alias → channel 路由链
参考: LLMGov 的 Prompt Registry & Router
"""

import time
import asyncio
import logging
from typing import Optional

from src.config import config
from src.models.schemas import ChatCompletionRequest, ChatCompletionResponse
from src.gateway.circuit_breaker import breaker_manager
from src.gateway.load_balancer import load_balancer
from src.gateway.provider import provider_manager, UpstreamError

logger = logging.getLogger("gateway.router")


class RouteResult:
    """路由结果"""

    def __init__(self):
        self.response: Optional[ChatCompletionResponse] = None
        self.provider: str = ""
        self.channel_id: str = ""
        self.latency_ms: float = 0.0
        self.success: bool = False
        self.error: Optional[str] = None
        self.attempted_channels: list[str] = []
        self.model_used: str = ""
        # 失败原因（上层据此映射 HTTP 状态码）：
        #   timeout / connect / upstream_5xx / upstream_429 / upstream_auth / upstream_4xx / unknown
        #   all_unavailable = 没有可用通道（全部熔断，或模型没配通道）
        self.failure_kind: str = ""


class StreamRouteResult:
    """流式路由结果。

    关键区别：非流式是"整段拿回来再判断成败"，流式是"先抢到第一个 chunk"。
    一旦 first_chunk 到手，通道就锁定了 —— 客户端已经能看到字节，再切会导致
    "前一半来自 A、后一半来自 B"的拼接错乱。
    """

    def __init__(self):
        self.agen = None                    # 上游 chunk 生成器（已吐出第一个 chunk）
        self.first_chunk: Optional[dict] = None
        self.provider: str = ""
        self.channel_id: str = ""
        self.attempted_channels: list[str] = []
        self.failure_kind: str = ""
        self.error: Optional[str] = None
        self.ttft_ms: float = 0.0           # 首 token 延迟
        self.success: bool = False


class ModelRouter:
    """
    模型路由器
    1. 解析模型别名
    2. 查找所有支持该模型的通道（按优先级排序）
    3. 负载均衡器选择一个健康通道
    4. 断路器检查是否允许调用
    5. 调用提供商适配器
    6. 记录成功/失败到断路器和负载均衡器
    7. 失败时尝试下一个通道（故障转移）
    """

    async def route(self, request: ChatCompletionRequest) -> RouteResult:
        result = RouteResult()
        result.model_used = config.model_aliases.get(request.model, request.model)

        # 查找候选通道
        candidates_raw = config.find_channels_for_model(request.model)

        if not candidates_raw:
            result.error = f"No provider channel found for model: {request.model}"
            result.failure_kind = "all_unavailable"      # 没配通道 = 我现在没能力服务
            logger.error(result.error)
            result.response = self._no_provider_response(request.model)
            return result

        # 构建候选列表
        candidates = []
        for provider_cfg, channel in candidates_raw:
            candidates.append({
                "provider": provider_cfg.name,
                "channel": channel,
                "channel_id": channel.id,
                "weight": channel.weight,
            })

        # 故障转移: 最多尝试所有候选通道
        max_attempts = len(candidates)

        for attempt in range(max_attempts):
            # 负载均衡选择
            selected = load_balancer.select(candidates)
            if selected is None:
                result.error = "No available channel (all circuit breakers open)"
                result.failure_kind = "all_unavailable"   # 全部熔断 = 我现在没能力服务
                break

            channel_id = selected["channel_id"]
            provider_name = selected["provider"]
            channel = selected["channel"]
            result.attempted_channels.append(channel_id)

            # 断路器检查
            cb = breaker_manager.get(channel_id)
            if not await cb.can_execute():
                logger.info(f"[{channel_id}] Circuit breaker open, skipping")
                # 从候选中移除该通道
                candidates = [c for c in candidates if c["channel_id"] != channel_id]
                continue

            # 调用提供商
            adapter = provider_manager.get(provider_name)
            if adapter is None:
                candidates = [c for c in candidates if c["channel_id"] != channel_id]
                continue

            # 替换为实际模型名:
            # 1. 先做全局别名解析 (model_aliases)
            # 2. 再做通道级模型映射 (channel.model_map): 虚拟模型名 → 该通道真实模型名
            routed_request = request.model_copy()
            upstream_model = result.model_used
            if upstream_model in channel.model_map:
                upstream_model = channel.model_map[upstream_model]
                logger.info(
                    f"[{channel_id}] model map: {result.model_used} → {upstream_model}"
                )
            routed_request.model = upstream_model

            logger.info(
                f"Routing to {provider_name}/{channel_id} "
                f"(model={upstream_model}, attempt={attempt+1}/{max_attempts})"
            )

            # 调用提供商：失败会抛 UpstreamError（返回值表达结果，异常表达失败）
            try:
                response, latency_ms, success = await adapter.chat_completion(channel, routed_request)
            except UpstreamError as e:
                response, success = None, False
                latency_ms = e.latency_ms
                result.failure_kind = e.kind
                result.error = e.message
                logger.warning(
                    f"[{channel_id}] {e.kind}: {e.message[:120]} (attempt {attempt+1})"
                )

            result.response = response
            result.provider = provider_name
            result.channel_id = channel_id
            result.latency_ms = latency_ms
            result.success = success

            # 记录到断路器
            if success:
                await cb.record_success()
                load_balancer.get_health(channel_id).record_latency(latency_ms, True)
                break  # 成功则不再尝试
            else:
                await cb.record_failure()
                load_balancer.get_health(channel_id).record_latency(latency_ms, False)
                if result.failure_kind == "":
                    result.failure_kind = "unknown"
                if response is not None:
                    result.error = response.choices[0].message.content if response.choices else result.error
                logger.warning(
                    f"[{channel_id}] Request failed (attempt {attempt+1}), "
                    f"trying next channel..."
                )
                # 从候选中移除，尝试下一个
                candidates = [c for c in candidates if c["channel_id"] != channel_id]

        return result

    # ─────────────────────────── 流式路由 ───────────────────────────
    async def route_stream(
        self,
        request: ChatCompletionRequest,
        first_token_timeout: float | None = None,
    ) -> StreamRouteResult:
        """流式路由：一路尝试到"抢到第一个 chunk"为止。

        与非流式的本质区别：
          · 非流式：任何时刻失败都能重发（客户端什么都没收到）
          · 流式：**只在"第一个 chunk 到手之前"能切通道**；之后只能流内报错
        所以这里的超时是 first_token_timeout（默认 5s），不是 provider 的总 timeout。
        """
        result = StreamRouteResult()
        timeout = (
            first_token_timeout
            if first_token_timeout is not None
            else getattr(getattr(config, "streaming", None), "first_token_timeout", 5.0)
        )
        resolved_model = config.model_aliases.get(request.model, request.model)

        candidates_raw = config.find_channels_for_model(request.model)
        if not candidates_raw:
            result.failure_kind = "all_unavailable"
            result.error = f"No provider channel found for model: {request.model}"
            return result

        candidates = [
            {
                "provider": p.name,
                "channel": ch,
                "channel_id": ch.id,
                "weight": ch.weight,
            }
            for p, ch in candidates_raw
        ]
        max_attempts = len(candidates)

        for attempt in range(max_attempts):
            selected = load_balancer.select(candidates)
            if selected is None:
                result.failure_kind = result.failure_kind or "all_unavailable"
                result.error = result.error or "No available channel (all circuit breakers open)"
                break

            channel_id = selected["channel_id"]
            provider_name = selected["provider"]
            channel = selected["channel"]
            result.attempted_channels.append(channel_id)

            cb = breaker_manager.get(channel_id)
            if not await cb.can_execute():
                logger.info(f"[stream] [{channel_id}] Circuit breaker open, skipping")
                candidates = [c for c in candidates if c["channel_id"] != channel_id]
                continue

            adapter = provider_manager.get(provider_name)
            if adapter is None:
                candidates = [c for c in candidates if c["channel_id"] != channel_id]
                continue

            routed_request = request.model_copy()
            upstream_model = resolved_model
            if upstream_model in channel.model_map:
                upstream_model = channel.model_map[upstream_model]
                logger.info(f"[stream] [{channel_id}] model map: {resolved_model} → {upstream_model}")
            routed_request.model = upstream_model

            logger.info(
                f"[stream] Routing to {provider_name}/{channel_id} "
                f"(model={upstream_model}, attempt={attempt+1}/{max_attempts}, "
                f"first_token_timeout={timeout}s)"
            )

            started = time.time()
            agen = adapter.chat_completion_stream(channel, routed_request)
            try:
                # ⭐ 全项目的关键一行：等第一个 chunk，超时就切通道
                first = await asyncio.wait_for(agen.__anext__(), timeout=timeout)
            except asyncio.TimeoutError:
                # 首 token 超时：客户端一个字都还没收到 → 切换是安全的
                await agen.aclose()
                await cb.record_failure()
                result.failure_kind = "timeout"
                result.error = f"[{channel_id}] 首 token 超时（>{timeout}s）"
                logger.warning(f"[stream] [{channel_id}] first token timeout, switching channel")
                candidates = [c for c in candidates if c["channel_id"] != channel_id]
                continue
            except StopAsyncIteration:
                await cb.record_failure()
                result.failure_kind = result.failure_kind or "unknown"
                result.error = f"[{channel_id}] 上游没有返回任何数据"
                candidates = [c for c in candidates if c["channel_id"] != channel_id]
                continue
            except UpstreamError as e:
                await cb.record_failure()
                result.failure_kind = e.kind
                result.error = e.message
                logger.warning(f"[stream] [{channel_id}] {e.kind}, switching channel")
                candidates = [c for c in candidates if c["channel_id"] != channel_id]
                continue

            # ✅ 首 chunk 到手 → 锁定这个通道，后面不再有切换机会
            await cb.record_success()
            load_balancer.get_health(channel_id).record_latency((time.time() - started) * 1000, True)
            result.agen = agen
            result.first_chunk = first
            result.provider = provider_name
            result.channel_id = channel_id
            result.ttft_ms = (time.time() - started) * 1000
            result.success = True
            break

        if not result.success and not result.failure_kind:
            result.failure_kind = "unknown"
        return result

    def _no_provider_response(self, model: str) -> ChatCompletionResponse:
        from src.models.schemas import Choice, Message, Usage
        return ChatCompletionResponse(
            id=f"noroute-{int(time.time())}",
            created=int(time.time()),
            model=model,
            choices=[Choice(
                index=0,
                message=Message(
                    role="assistant",
                    content=f"[Gateway] No provider configured for model '{model}'. "
                            f"Please check config/providers.yaml.",
                ),
                finish_reason="stop",
            )],
            usage=Usage(),
        )


# 全局实例
router = ModelRouter()

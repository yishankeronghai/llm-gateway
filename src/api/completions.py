"""
Chat Completions API 端点
OpenAI 兼容的 /v1/chat/completions 接口
这是网关的核心代理端点，串联所有模块

请求处理管道:
  Auth → Rate Limit → Prompt Resolution → Cache Check → Router → Provider → Audit
"""

import json
import time
import uuid
import logging

from fastapi import APIRouter, Request, HTTPException, Header
from fastapi.responses import JSONResponse, StreamingResponse

from src.models.schemas import ChatCompletionRequest, ChatCompletionResponse
from src.middleware.auth import auth_manager
from src.middleware.rate_limit import rate_limiter
from src.prompt.registry import prompt_registry
from src.cache.response_cache import response_cache
from src.gateway.router import router
from src.audit.logger import audit_logger

logger = logging.getLogger("api.completions")

# ── 失败原因 → HTTP 状态码 ──────────────────────────────────────
# 504：上游太慢（我们等超时了）
# 502：上游给了坏响应 / 连不上（网关的上游坏了）
# 503：我自己现在没能力服务（所有通道熔断、或模型没配通道）
STATUS_BY_KIND = {
    "timeout": 504,
    "connect": 502,
    "upstream_5xx": 502,
    "upstream_429": 502,
    "upstream_auth": 502,
    "upstream_4xx": 502,
    "unknown": 502,
    "all_unavailable": 503,
}

# 失败原因 → 机器可读的错误码（调用方据此分支，不要解析中文 message）
ERROR_CODE_BY_KIND = {
    "timeout": "upstream_timeout",
    "connect": "upstream_unreachable",
    "upstream_5xx": "upstream_server_error",
    "upstream_429": "upstream_rate_limited",
    "upstream_auth": "upstream_auth_failed",
    "upstream_4xx": "upstream_bad_request",
    "unknown": "upstream_unknown",
    "all_unavailable": "no_available_channel",
}

router_api = APIRouter()


@router_api.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str = Header(None),
):
    """
    OpenAI 兼容的 Chat Completions 端点

    功能:
    1. API Key 认证
    2. 限流检查
    3. 提示词模板解析 (如果指定了 prompt_id)
    4. 响应缓存检查
    5. 智能路由 + 负载均衡 + 故障转移
    6. 审计日志记录
    """
    trace_id = str(uuid.uuid4())[:8]
    start_time = time.time()

    # ── 1. 认证 ──
    api_key = auth_manager.extract_key(authorization)
    auth_ok, auth_info = auth_manager.verify(api_key)
    if not auth_ok:
        logger.warning(f"[{trace_id}] Auth failed: {auth_info}")
        raise HTTPException(status_code=401, detail=f"Authentication failed: {auth_info}")

    # ── 2. 限流 ──
    allowed, rate_info = await rate_limiter.check(auth_info)
    if not allowed:
        logger.warning(f"[{trace_id}] Rate limited: {rate_info}")
        raise HTTPException(
            status_code=429,
            detail=rate_info,
            headers={"Retry-After": str(rate_info.get("retry_after", 1))},
        )

    # ── 3. 解析请求体 ──
    try:
        body = await request.json()
        req = ChatCompletionRequest(**body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid request body: {e}")

    # ── 4. 提示词模板解析 ──
    prompt_id = None
    prompt_version = None
    if req.prompt_id:
        try:
            system_prompt, prompt_id, prompt_version = prompt_registry.resolve(
                req.prompt_id,
                req.prompt_vars or {},
            )
            # 将系统提示词注入到消息列表前面
            from src.models.schemas import Message
            req.messages.insert(0, Message(role="system", content=system_prompt))
            logger.info(
                f"[{trace_id}] Prompt resolved: {prompt_id}@{prompt_version}"
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ── 5. 流式分支（stream=true 走这里；缓存不参与流式）──
    if req.stream:
        return await _handle_stream(
            req=req,
            trace_id=trace_id,
            auth_info=auth_info,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            start_time=start_time,
        )

    # ── 6. 缓存检查 ──
    cached = response_cache.get(req)
    if cached:
        logger.info(f"[{trace_id}] Cache HIT")
        latency_ms = (time.time() - start_time) * 1000
        # 记录缓存命中的审计日志
        await audit_logger.log(
            trace_id=trace_id,
            api_key=auth_info,
            model=req.model,
            provider="cache",
            channel_id="cache-hit",
            latency_ms=latency_ms,
            status_code=200,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
        )
        return JSONResponse(content=cached.model_dump())

    # ── 6. 路由 + 调用 ──
    route_result = await router.route(req)
    latency_ms = (time.time() - start_time) * 1000

    # ── 7. 缓存写入 (仅成功时) ──
    if route_result.success and route_result.response:
        response_cache.set(req, route_result.response)

    # ── 8. 决定 HTTP 状态码（失败语义映射）──
    # 关键改动：以前只要 route_result.response 有值就返回 200，
    # 而上游失败时 response 也有值（装着错误文本）→ 于是"上游挂了却返回 200"。
    ok = route_result.success and route_result.response is not None
    kind = route_result.failure_kind or ("unknown" if not ok else "")
    status_code = 200 if ok else STATUS_BY_KIND.get(kind, 502)

    # ── 9. 审计日志（记录真实状态码，别再一律写 500）──
    usage = route_result.response.usage if route_result.response else None
    await audit_logger.log(
        trace_id=trace_id,
        api_key=auth_info,
        model=req.model,
        provider=route_result.provider or "none",
        channel_id=route_result.channel_id or "none",
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        total_tokens=usage.total_tokens if usage else 0,
        latency_ms=latency_ms,
        status_code=status_code,
        error=route_result.error,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
    )

    # ── 10. 返回响应 ──
    if not ok:
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": ERROR_CODE_BY_KIND.get(kind, "upstream_unknown"),
                    "kind": kind,
                    "message": route_result.error or "所有通道均失败",
                    "trace_id": trace_id,
                    "attempted_channels": route_result.attempted_channels,
                    "latency_ms": round(latency_ms, 2),
                }
            },
        )

    resp_data = route_result.response.model_dump()
    # 附加网关元信息
    resp_data["x_gateway"] = {
        "trace_id": trace_id,
        "provider": route_result.provider,
        "channel": route_result.channel_id,
        "latency_ms": round(latency_ms, 2),
        "cached": False,
        "attempted_channels": route_result.attempted_channels,
        "prompt": {"id": prompt_id, "version": prompt_version} if prompt_id else None,
    }
    return JSONResponse(content=resp_data)


async def _handle_stream(
    *,
    req: ChatCompletionRequest,
    trace_id: str,
    auth_info,
    prompt_id,
    prompt_version,
    start_time: float,
):
    """流式分支：把上游 chunk 透传给客户端。

    ══ 这一段是"你的部分"：4 个 NotImplementedError 处由你补 ══
    规则：只填 yield 语句，不改结构。
    """

    # 1) 先抢到"第一个 chunk"——这一步内部已经做了首 token 超时 + 通道切换
    route = await router.route_stream(req)
    latency_ms = (time.time() - start_time) * 1000

    # 2) 一个 token 都没发出去就失败了 → 状态码还能用，走和非流式一样的错误体
    if not route.success:
        kind = route.failure_kind or "unknown"
        status_code = STATUS_BY_KIND.get(kind, 502)
        await audit_logger.log(
            trace_id=trace_id,
            api_key=auth_info,
            model=req.model,
            provider=route.provider or "none",
            channel_id=route.channel_id or "none",
            latency_ms=latency_ms,
            status_code=status_code,
            error=route.error,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": ERROR_CODE_BY_KIND.get(kind, "upstream_unknown"),
                    "kind": kind,
                    "message": route.error or "所有通道均失败",
                    "trace_id": trace_id,
                    "attempted_channels": route.attempted_channels,
                }
            },
        )

    # 3) 成功抢到首 chunk → 开始真正流式输出（从这里起，状态码不能再改）
    async def gen():
        chunks_sent = 0
        try:
            # STREAM-1 · 第一个 chunk（首 token 已经在 route_stream 里拿到了）
            yield f"data: {json.dumps(route.first_chunk, ensure_ascii=False)}\n\n"
            chunks_sent += 1

            # STREAM-2 · 透传剩余 chunk。注意：这一段没有"换通道"的机会了，
            #            状态码和响应头早就发出去了，只能一条一条往外送。
            async for chunk in route.agen:
                if chunk.get("__done__"):        # 上游发了 [DONE]
                    break
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                chunks_sent += 1

            # STREAM-3 · 结束标记（客户端靠它判断"流完了"）
            yield "data: [DONE]\n\n"

        except Exception as e:
            # STREAM-4 · 已经发过内容后出错：状态码改不了，只能往流里塞错误事件，
            #            让客户端自己决定重试还是报错。
            err = {
                "error": {
                    "code": "stream_interrupted",
                    "message": str(e)[:200],
                    "trace_id": trace_id,
                    "chunks_sent": chunks_sent,
                }
            }
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    await audit_logger.log(
        trace_id=trace_id,
        api_key=auth_info,
        model=req.model,
        provider=route.provider,
        channel_id=route.channel_id,
        latency_ms=latency_ms,
        status_code=200,
        error=None,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
    )
    logger.info(
        f"[{trace_id}] stream start: provider={route.provider} channel={route.channel_id} "
        f"ttft={route.ttft_ms:.0f}ms attempted={route.attempted_channels}"
    )

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",          # 让 nginx 不缓冲（否则流式看起来像一次性返回）
        "x-gateway-channel": route.channel_id,
        "x-gateway-provider": route.provider,
        "x-gateway-ttft-ms": f"{route.ttft_ms:.0f}",
        "x-gateway-attempted": ",".join(route.attempted_channels),
    }
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


@router_api.post("/v1/chat/completions/dry-run")
async def chat_completions_dry_run(request: Request):
    """Dry-run: 仅渲染提示词模板，不调用模型"""
    body = await request.json()
    prompt_id = body.get("prompt_id")
    prompt_vars = body.get("prompt_vars", {})

    if not prompt_id:
        raise HTTPException(status_code=400, detail="prompt_id is required for dry-run")

    result = prompt_registry.dry_run(prompt_id, prompt_vars)
    return JSONResponse(content=result)

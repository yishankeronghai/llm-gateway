"""
提供商适配器 (Provider Adapter)
将不同 LLM 提供商的 API 统一为 OpenAI 兼容格式
负责实际的 HTTP 调用

参考: AIGC Gateway 的 adapter 层设计
参考: LLMGov 的 Provider Abstraction
"""

from __future__ import annotations

import json
import time
import httpx
import asyncio
import logging
from typing import Optional

from src.config import config, ProviderConfig, ChannelConfig
from src.models.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Message,
    Usage,
)

logger = logging.getLogger("gateway.provider")


class UpstreamError(Exception):
    """上游调用失败。

    设计原则：**返回值表达结果，异常表达失败**。
    以前把错误包成一个正常的响应体返回（success=False + 错误文本），
    导致上层还能把它当成 200 返回给调用方 —— 那就是"上游失败却返回 200"的根因。

    kind 用于让上层把失败映射成正确的 HTTP 状态码：
        timeout / connect / upstream_5xx / upstream_429 / upstream_auth / upstream_4xx / unknown
    """

    def __init__(
        self,
        kind: str,
        message: str,
        status_code: int | None = None,
        latency_ms: float = 0.0,
    ):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code
        self.latency_ms = latency_ms



class ProviderAdapter:
    """
    OpenAI 兼容的提供商适配器
    所有提供商统一走 /v1/chat/completions 端点
    """

    def __init__(self, provider: ProviderConfig):
        self.provider = provider
        self.client = httpx.AsyncClient(
            base_url=provider.base_url,
            timeout=provider.timeout,
            headers={
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def chat_completion(
        self, channel: ChannelConfig, request: ChatCompletionRequest
    ) -> tuple[ChatCompletionResponse, float, bool]:
        """
        调用上游 LLM API
        返回: (响应, 延迟ms, 是否成功)
        """
        url = "/chat/completions"
        payload = {
            "model": request.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": request.temperature,
        }
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        if request.top_p:
            payload["top_p"] = request.top_p

        start = time.time()
        try:
            resp = await self.client.post(url, json=payload)
            latency_ms = (time.time() - start) * 1000

            if resp.status_code != 200:
                error_text = resp.text
                # 按状态码给失败分类，上层据此映射 HTTP 状态码
                if resp.status_code >= 500:
                    kind = "upstream_5xx"
                elif resp.status_code == 429:
                    kind = "upstream_429"
                elif resp.status_code in (401, 403):
                    kind = "upstream_auth"
                elif resp.status_code >= 400:
                    kind = "upstream_4xx"
                else:
                    kind = "upstream_5xx"
                logger.error(
                    f"[{self.provider.name}/{channel.id}] "
                    f"HTTP {resp.status_code} ({kind}): {error_text[:200]}"
                )
                # 不再把错误包装成"正常的响应体"，而是抛异常让上层做状态码映射
                raise UpstreamError(
                    kind,
                    f"[{channel.id}] HTTP {resp.status_code}: {error_text[:300]}",
                    resp.status_code,
                    latency_ms,
                )

            data = resp.json()
            # 统一为 OpenAI 格式
            response = ChatCompletionResponse(
                id=data.get("id", f"chatcmpl-{int(time.time())}"),
                created=data.get("created", int(time.time())),
                model=data.get("model", request.model),
                choices=[
                    Choice(
                        index=i,
                        message=Message(
                            role=choice["message"].get("role", "assistant"),
                            content=choice["message"].get("content", ""),
                        ),
                        finish_reason=choice.get("finish_reason", "stop"),
                    )
                    for i, choice in enumerate(data.get("choices", []))
                ],
                usage=Usage(
                    prompt_tokens=data.get("usage", {}).get("prompt_tokens", 0),
                    completion_tokens=data.get("usage", {}).get("completion_tokens", 0),
                    total_tokens=data.get("usage", {}).get("total_tokens", 0),
                ),
            )
            return response, latency_ms, True

        except asyncio.TimeoutError:
            latency_ms = (time.time() - start) * 1000
            logger.error(f"[{self.provider.name}/{channel.id}] Timeout after {self.provider.timeout}s")
            raise UpstreamError(
                "timeout",
                f"[{channel.id}] timeout after {self.provider.timeout}s",
                None,
                latency_ms,
            )
        except httpx.TimeoutException:
            latency_ms = (time.time() - start) * 1000
            logger.error(f"[{self.provider.name}/{channel.id}] httpx Timeout")
            raise UpstreamError(
                "timeout",
                f"[{channel.id}] timeout after {self.provider.timeout}s",
                None,
                latency_ms,
            )
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
            latency_ms = (time.time() - start) * 1000
            logger.error(f"[{self.provider.name}/{channel.id}] Connection error: {e}")
            raise UpstreamError(
                "connect",
                f"[{channel.id}] 连接失败: {e}",
                None,
                latency_ms,
            )
        except httpx.HTTPError as e:
            latency_ms = (time.time() - start) * 1000
            logger.error(f"[{self.provider.name}/{channel.id}] HTTP error: {e}")
            raise UpstreamError(
                "connect",
                f"[{channel.id}] HTTP 层错误: {e}",
                None,
                latency_ms,
            )
        except Exception as e:
            latency_ms = (time.time() - start) * 1000
            logger.error(f"[{self.provider.name}/{channel.id}] Exception: {e}")
            raise UpstreamError("unknown", f"[{channel.id}] {e}", None, latency_ms)

    # ─────────────────────────── 流式 ───────────────────────────
    async def chat_completion_stream(
        self, channel: ChannelConfig, request: ChatCompletionRequest
    ):
        """流式调用上游，逐个 yield 上游返回的 `data:` JSON（已解析成 dict）。

        协议（OpenAI SSE）：
            data: {"choices":[{"delta":{"content":"你"}}]}
            data: {"choices":[{"delta":{"content":"好"}}]}
            data: [DONE]

        行为约定（和 router.route_stream 配合）：
          · 上游 HTTP 非 200 / 连不上 / 超时 → 抛 UpstreamError（和谁都没发出内容时一致）
          · 一旦开始 yield，上层就不能再"换通道重发"了（客户端已经收到字节）
        """
        url = "/chat/completions"
        payload = {
            "model": request.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": request.temperature,
            "stream": True,
        }
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        if request.top_p:
            payload["top_p"] = request.top_p

        start = time.time()
        try:
            async with self.client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    raw = await resp.aread()
                    text = raw.decode("utf-8", "replace")
                    kind = (
                        "upstream_5xx" if resp.status_code >= 500
                        else "upstream_429" if resp.status_code == 429
                        else "upstream_auth" if resp.status_code in (401, 403)
                        else "upstream_4xx" if resp.status_code >= 400
                        else "upstream_5xx"
                    )
                    logger.error(f"[{self.provider.name}/{channel.id}] stream HTTP {resp.status_code} ({kind}): {text[:200]}")
                    raise UpstreamError(
                        kind,
                        f"[{channel.id}] HTTP {resp.status_code}: {text[:300]}",
                        resp.status_code,
                        (time.time() - start) * 1000,
                    )

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue                      # 空行 / 注释行 / event: 行都跳过
                    data = line[5:].strip()
                    if data == "[DONE]":
                        yield {"__done__": True}
                        return
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning(f"[{channel.id}] 跳过无法解析的 SSE 行: {data[:80]}")
                        continue

        except UpstreamError:
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise UpstreamError("timeout", f"[{channel.id}] stream timeout", None, (time.time() - start) * 1000)
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
            raise UpstreamError("connect", f"[{channel.id}] 流式连接失败: {e}", None, (time.time() - start) * 1000)
        except httpx.HTTPError as e:
            raise UpstreamError("connect", f"[{channel.id}] 流式 HTTP 层错误: {e}", None, (time.time() - start) * 1000)
        except Exception as e:
            raise UpstreamError("unknown", f"[{channel.id}] 流式中异常: {e}", None, (time.time() - start) * 1000)

    async def close(self):
        await self.client.aclose()


# ── 提供商适配器管理器 ────────────────────────────────────────

class ProviderManager:
    """管理所有提供商适配器"""

    def __init__(self):
        self._adapters: dict[str, ProviderAdapter] = {}

    def init(self):
        for p in config.providers:
            self._adapters[p.name] = ProviderAdapter(p)
            logger.info(f"Provider adapter initialized: {p.name} → {p.base_url}")

    def get(self, provider_name: str) -> Optional[ProviderAdapter]:
        return self._adapters.get(provider_name)

    async def close_all(self):
        for adapter in self._adapters.values():
            await adapter.close()


# 全局实例
provider_manager = ProviderManager()

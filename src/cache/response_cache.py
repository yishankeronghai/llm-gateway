"""
响应缓存 (Response Cache)
基于请求内容哈希的缓存，减少重复调用成本

参考: LLMGov 的语义缓存 (cache.py, cache_keys.py)
参考: Helicone AI Gateway 的缓存机制 (最高 95% 成本降低)
"""

import hashlib
import time
import json
import logging
from typing import Optional

from src.models.schemas import ChatCompletionRequest, ChatCompletionResponse

logger = logging.getLogger("cache.response")


class ResponseCache:
    """
    响应缓存
    - 基于 model + messages 的 SHA-256 哈希作为缓存键
    - TTL 过期机制
    - 命中率统计
    """

    def __init__(self, default_ttl: int = 300):
        self.default_ttl = default_ttl
        self._cache: dict[str, dict] = {}  # key -> {response, expire_at}
        self._hits = 0
        self._misses = 0

    def _make_key(self, request: ChatCompletionRequest) -> str:
        """构建缓存键"""
        content = json.dumps({
            "model": request.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": request.temperature,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def get(self, request: ChatCompletionRequest) -> Optional[ChatCompletionResponse]:
        """查询缓存"""
        key = self._make_key(request)
        if key in self._cache:
            entry = self._cache[key]
            if time.time() < entry["expire_at"]:
                self._hits += 1
                logger.debug(f"Cache HIT: {key[:12]}...")
                cached = entry["response"]
                # 返回缓存的副本
                return cached.model_copy(deep=True)
            else:
                # 过期，移除
                del self._cache[key]

        self._misses += 1
        return None

    def set(self, request: ChatCompletionRequest, response: ChatCompletionResponse, ttl: int = None):
        """写入缓存"""
        key = self._make_key(request)
        self._cache[key] = {
            "response": response,
            "expire_at": time.time() + (ttl or self.default_ttl),
        }
        logger.debug(f"Cache SET: {key[:12]}... (ttl={ttl or self.default_ttl}s)")

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "cache_size": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total * 100, 2) if total else 0,
        }

    def clear(self):
        self._cache.clear()
        self._hits = 0
        self._misses = 0


# 全局实例
response_cache = ResponseCache()

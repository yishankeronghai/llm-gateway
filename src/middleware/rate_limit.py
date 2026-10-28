"""
限流中间件 (Rate Limiter)
滑动窗口算法，按 API Key 维度限流

参考: LLMGov 的故障开启式 Redis 限流器 (rate_limiter.py)
参考: PromptShield Gateway 的 rate limiting
"""

import time
import asyncio
from collections import defaultdict, deque
from typing import Optional
import logging

from src.config import config

logger = logging.getLogger("middleware.rate_limit")


class SlidingWindowLimiter:
    """
    滑动窗口限流器
    在时间窗口内统计请求数，超过限制则拒绝
    故障开启: 内部异常时放行 (避免因限流器故障导致服务不可用)
    """

    def __init__(self, rpm: int = 60):
        self.rpm = rpm
        self._windows: dict[str, deque] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> tuple[bool, dict]:
        """
        检查是否允许请求
        返回: (是否允许, 限流信息)
        """
        try:
            async with self._lock:
                now = time.time()
                window = self._windows[key]

                # 清除窗口外的记录
                while window and window[0] <= now - 60:
                    window.popleft()

                current_count = len(window)
                if current_count >= self.rpm:
                    retry_after = int(60 - (now - window[0]))
                    return False, {
                        "error": "rate_limit_exceeded",
                        "message": f"Rate limit exceeded: {current_count}/{self.rpm} rpm",
                        "retry_after": max(retry_after, 1),
                    }

                window.append(now)
                return True, {
                    "remaining": self.rpm - current_count - 1,
                    "limit": self.rpm,
                }
        except Exception as e:
            # 故障开启: 出错时放行
            logger.error(f"Rate limiter error (fail-open): {e}")
            return True, {"remaining": -1, "limit": self.rpm, "error": str(e)}


class RateLimiter:
    """限流管理器"""

    def __init__(self):
        self._limiters: dict[str, SlidingWindowLimiter] = {}

    def get(self, key: str) -> SlidingWindowLimiter:
        if key not in self._limiters:
            self._limiters[key] = SlidingWindowLimiter(config.rate_limiting.default_rpm)
        return self._limiters[key]

    async def check(self, api_key: str) -> tuple[bool, dict]:
        """检查 API Key 的限流"""
        if not config.rate_limiting.per_key:
            return True, {"remaining": -1, "limit": -1}
        limiter = self.get(api_key)
        return await limiter.allow(api_key)


# 全局实例
rate_limiter = RateLimiter()

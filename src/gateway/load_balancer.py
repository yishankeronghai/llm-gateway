"""
智能负载均衡器
实现多种负载均衡策略: 加权轮询、最低延迟、轮询、随机

参考: Helicone AI Gateway 的 P2C (Power of Two Choices) 策略
参考: AIGC Gateway 的优先级 + 健康状态路由
"""

import time
import random
import itertools
from typing import Optional
import logging

logger = logging.getLogger("gateway.load_balancer")


class ChannelHealth:
    """通道健康状态记录"""

    def __init__(self, channel_id: str):
        self.channel_id = channel_id
        self.healthy: bool = True
        self.avg_latency_ms: float = 0.0
        self._latencies: list[float] = []
        self._max_samples = 100
        self.error_rate: float = 0.0
        self._total_requests = 0
        self._total_errors = 0
        self.last_check: float = time.time()

    def record_latency(self, latency_ms: float, success: bool):
        self._total_requests += 1
        if not success:
            self._total_errors += 1
        self._latencies.append(latency_ms)
        if len(self._latencies) > self._max_samples:
            self._latencies.pop(0)
        # 滑动平均
        self.avg_latency_ms = sum(self._latencies) / len(self._latencies) if self._latencies else 0
        self.error_rate = self._total_errors / self._total_requests if self._total_requests else 0
        self.last_check = time.time()

    def mark_unhealthy(self):
        self.healthy = False

    def mark_healthy(self):
        self.healthy = True

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "healthy": self.healthy,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "error_rate": round(self.error_rate, 4),
            "total_requests": self._total_requests,
            "total_errors": self._total_errors,
        }


class LoadBalancer:
    """
    智能负载均衡器
    根据策略从候选通道中选择一个健康通道
    """

    def __init__(self, strategy: str = "weighted_round_robin"):
        self.strategy = strategy
        self._health_records: dict[str, ChannelHealth] = {}
        self._round_robin_iter: dict[str, itertools.cycle] = {}
        self._weights: dict[str, list] = {}  # channel_id -> expanded weight list

    def get_health(self, channel_id: str) -> ChannelHealth:
        if channel_id not in self._health_records:
            self._health_records[channel_id] = ChannelHealth(channel_id)
        return self._health_records[channel_id]

    def select(
        self,
        candidates: list[dict],  # [{"provider": str, "channel": ChannelConfig, "channel_id": str}]
    ) -> Optional[dict]:
        """
        从候选通道中选择一个
        过滤掉断路器打开的和不健康的通道
        主备语义: 优先在 priority 最小的通道组内做负载均衡,
                 (如 DeepSeek priority=1, OpenAI priority=2 → 总是先走 DeepSeek,
                   DeepSeek 熔断/不健康后才切到 OpenAI)
        """
        from src.gateway.circuit_breaker import breaker_manager

        def _priority(c: dict) -> int:
            ch = c.get("channel")
            return ch.priority if ch is not None else 1

        # 过滤: 断路器放行 + 健康状态
        available = []
        for c in candidates:
            cb = breaker_manager.get(c["channel_id"])
            health = self.get_health(c["channel_id"])
            if cb.state.value == "closed" and health.healthy:
                available.append(c)

        if not available:
            # 所有通道都不可用，尝试半开通道
            for c in candidates:
                cb = breaker_manager.get(c["channel_id"])
                if cb.state.value == "half_open":
                    available.append(c)

        if not available:
            logger.warning("No available channels for routing")
            return None

        # 主备语义: 只在最低 priority 的通道组内做负载均衡
        min_priority = min(_priority(c) for c in available)
        preferred = [c for c in available if _priority(c) == min_priority]
        pool = preferred or available

        if self.strategy == "round_robin":
            return self._round_robin(pool)
        elif self.strategy == "weighted_round_robin":
            return self._weighted_round_robin(pool)
        elif self.strategy == "least_latency":
            return self._least_latency(pool)
        elif self.strategy == "random":
            return random.choice(pool)
        else:
            return self._weighted_round_robin(pool)

    def _round_robin(self, candidates: list[dict]) -> dict:
        key = ",".join(c["channel_id"] for c in candidates)
        if key not in self._round_robin_iter:
            self._round_robin_iter[key] = itertools.cycle(candidates)
        return next(self._round_robin_iter[key])

    def _weighted_round_robin(self, candidates: list[dict]) -> dict:
        """
        加权轮询: 按权重展开为列表，轮询选取
        同时考虑延迟因子做智能调整
        """
        # 构建加权列表
        key = ",".join(c["channel_id"] for c in candidates)
        if key not in self._weights:
            expanded = []
            for c in candidates:
                weight = c.get("weight", 100)
                # 归一化权重到 1-10 范围
                normalized = max(1, weight // 10)
                expanded.extend([c] * normalized)
            self._weights[key] = expanded

        pool = self._weights[key]
        if not pool:
            return candidates[0]
        idx = random.randint(0, len(pool) - 1)
        return pool[idx]

    def _least_latency(self, candidates: list[dict]) -> dict:
        """选择平均延迟最低的通道"""
        def get_latency(c):
            h = self.get_health(c["channel_id"])
            return h.avg_latency_ms if h.avg_latency_ms > 0 else 9999
        return min(candidates, key=get_latency)

    def all_health_status(self) -> list[dict]:
        return [h.to_dict() for h in self._health_records.values()]


# 全局实例
load_balancer = LoadBalancer()

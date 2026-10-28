"""W2 · 亲手重写的断路器（B 方案参考实现）

这一版由我写，但**你要能逐行讲出来**——写完我会考你 3 个问题。
判分：.venv\\Scripts\\python.exe experiments\\w2_breaker_check.py

三段关键设计，读的时候重点看：
  1. state 属性：惰性晋升（读的时候顺手检查冷却是否到期）
  2. can_execute：必须读 self.state（否则永远看不到半开）
  3. record_success / record_failure：只有"有证据"才允许状态跃迁
"""

import asyncio
import time
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "closed"        # 正常：全部放行
    OPEN = "open"            # 熔断：全部拒绝
    HALF_OPEN = "half_open"  # 半开：只放少量探测


class CircuitBreaker:
    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        half_open_max_calls: int = 1,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        # ── 类的全部内存（4 个字段）──
        self._state = CircuitState.CLOSED   # 当前状态
        self._failure_count = 0             # 连续失败次数（成功清零）
        self._last_failure_at = None        # 上次失败的时间戳；None = 从没失败过
        self._half_open_calls = 0           # 半开状态下已放行的探测数

        # Day 2 冲第 9 组时打开这行（现在不需要，判分器第 9 组也能过）
        # self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """返回当前状态；如果熔断中且冷却到期，顺手晋升为 HALF_OPEN。

        为什么放在"读取时"做？因为状态只在被观察时才有意义，
        省掉一个后台定时器线程，就省掉一整类定时器 bug。
        """
        if self._state == CircuitState.OPEN and self._last_failure_at is not None:
            if (time.time() - self._last_failure_at) >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0      # 新一轮半开，探测配额重新给满
        return self._state

    async def can_execute(self) -> bool:
        """要不要放这个请求过去。

        注意：读 self.state（属性），不是 self._state。
        只有走属性，上面那段"冷却到期 → 半开"的检查才会被执行。
        """
        s = self.state

        if s == CircuitState.CLOSED:
            return True

        if s == CircuitState.OPEN:
            return False

        # 走到这里必然是 HALF_OPEN：只放配额内的探测请求
        if self._half_open_calls < self.half_open_max_calls:
            self._half_open_calls += 1          # 先记账，
            return True                         # 再放行
        return False                            # 账满了

    async def record_success(self):
        """记一次成功。成功 = 有证据，所以可以把状态往"更信任"的方向推。"""
        if self.state == CircuitState.HALF_OPEN:
            # 探测成功 → 证明上游确实恢复了 → 全面恢复
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_calls = 0
            return

        # CLOSED 下成功 → 把连续失败计数清零
        # 否则"4 败 + 1 成 + 1 败"会被误判成"连续 5 次失败"而熔断
        self._failure_count = 0

    async def record_failure(self):
        """记一次失败。失败 = 有反向证据，所以可以把状态往"更拒绝"的方向推。"""

        # ① 先记下"这次失败发生在什么时候"——冷却时间全靠它算。
        #    注意：这一行放在最前面，所以半开探测失败时，
        #    时间戳被刷新 = 冷却自动重新计时（这就是第 6 组要的）
        self._last_failure_at = time.time()

        # ② 半开下失败 → 探测没过，说明上游还没恢复
        if self.state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            self._half_open_calls = 0
            return

        # ③ 正常状态下累计失败，够数就熔断（>= ：第 5 次失败当场熔断）
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
        }


class CircuitBreakerManager:
    """按通道名管理断路器：一个通道一个断路器，反复复用。"""

    def __init__(self):
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, channel_id: str) -> CircuitBreaker:
        if channel_id not in self._breakers:
            self._breakers[channel_id] = CircuitBreaker(name=channel_id)
        return self._breakers[channel_id]

    def all_status(self) -> list[dict]:
        return [cb.to_dict() for cb in self._breakers.values()]


# 全局实例（src/gateway/router.py 会用这个名字）
breaker_manager = CircuitBreakerManager()

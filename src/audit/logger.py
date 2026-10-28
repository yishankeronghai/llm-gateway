"""
审计日志记录器
将请求处理过程中的审计信息写入审计存储
异步写入，不阻塞主请求路径

参考: LLMGov 的异步 ClickHouse 指标写入器 (telemetry.py)
"""

import asyncio
import time
import logging
from datetime import datetime, timezone
from typing import Optional

from src.audit.store import AuditStore
from src.config import config

logger = logging.getLogger("audit.logger")


class AuditLogger:
    """
    审计日志记录器
    - 异步写入审计日志 (不阻塞热路径)
    - 记录请求全链路信息
    - 维护防篡改哈希链
    """

    def __init__(self):
        self._store: Optional[AuditStore] = None
        self._queue: Optional[asyncio.Queue] = None
        self._writer_task: Optional[asyncio.Task] = None

    def init(self):
        self._store = AuditStore(config.db_path)
        # 在当前事件循环中创建队列 (不能在 __init__ 中创建，否则会绑定到错误的循环)
        self._queue = asyncio.Queue()
        # 启动异步写入协程
        self._writer_task = asyncio.create_task(self._writer_loop())
        logger.info("AuditLogger started (async writer loop)")

    async def _writer_loop(self):
        """异步写入循环"""
        while True:
            entry = await self._queue.get()
            try:
                self._store.append(entry)
            except Exception as e:
                logger.error(f"Audit write failed: {e}")

    async def log(
        self,
        trace_id: str,
        api_key: str,
        model: str,
        provider: str,
        channel_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        latency_ms: float = 0.0,
        status_code: int = 200,
        error: Optional[str] = None,
        prompt_id: Optional[str] = None,
        prompt_version: Optional[str] = None,
        request_content: Optional[str] = None,
    ):
        """记录一条审计日志 (非阻塞)"""
        if self._queue is None:
            logger.warning("AuditLogger not initialized, skipping log")
            return
        entry = {
            "trace_id": trace_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "api_key": api_key[:8] + "***" if api_key else "anonymous",  # 脱敏
            "model": model,
            "provider": provider,
            "channel_id": channel_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "latency_ms": round(latency_ms, 2),
            "status_code": status_code,
            "error": error,
            "prompt_id": prompt_id,
            "prompt_version": prompt_version,
            "request_content": request_content,
        }
        await self._queue.put(entry)

    @property
    def store(self) -> Optional[AuditStore]:
        return self._store


# 全局实例
audit_logger = AuditLogger()

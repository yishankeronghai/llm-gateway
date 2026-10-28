"""
认证中间件 (Authentication)
API Key 验证

参考: LLMGov 的故障关闭式 API Key 验证 (auth.py)
参考: AIGC Gateway 的 Bearer token auth-middleware
"""

import os
import logging
from typing import Optional

logger = logging.getLogger("middleware.auth")


class AuthManager:
    """
    认证管理器
    支持通过环境变量配置有效的 API Key
    """

    def __init__(self):
        # 从环境变量加载有效 API Keys
        env_keys = os.environ.get("GATEWAY_API_KEYS", "")
        self._valid_keys: set[str] = set()
        if env_keys:
            self._valid_keys = set(k.strip() for k in env_keys.split(",") if k.strip())

        # 是否启用认证 (没有配置 key 时禁用，方便本地开发)
        self.enabled = len(self._valid_keys) > 0

        if self.enabled:
            logger.info(f"Auth enabled with {len(self._valid_keys)} API key(s)")
        else:
            logger.warning("Auth disabled (no GATEWAY_API_KEYS set) — open access mode")

    def verify(self, api_key: Optional[str]) -> tuple[bool, str]:
        """
        验证 API Key
        返回: (是否通过, api_key 或错误原因)
        """
        if not self.enabled:
            # 开放模式
            return True, "dev-mode"

        if not api_key:
            return False, "missing_api_key"

        if api_key in self._valid_keys:
            return True, api_key
        else:
            return False, "invalid_api_key"

    def extract_key(self, authorization_header: Optional[str]) -> Optional[str]:
        """从 Authorization 头提取 API Key"""
        if not authorization_header:
            return None
        if authorization_header.startswith("Bearer "):
            return authorization_header[7:]
        return authorization_header


# 全局实例
auth_manager = AuthManager()

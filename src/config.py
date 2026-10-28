"""
配置管理模块
负责加载 YAML 配置文件并提供统一的配置访问接口
"""
from __future__ import annotations

import os
import yaml
from pathlib import Path
from pydantic import BaseModel, ConfigDict
from typing import Optional


# ── 数据结构定义 ──────────────────────────────────────────────

class ChannelConfig(BaseModel):
    # 允许 "model_map" 等以 model_ 开头的字段名, 避免 pydantic 保护命名空间警告
    model_config = ConfigDict(protected_namespaces=())

    id: str
    priority: int = 1
    weight: int = 100
    models: list[str] = []
    # 虚拟模型名 → 该通道真实模型名 (用于跨提供商故障转移)
    # 例: {"auto-chat": "deepseek-chat"} 表示请求 auto-chat 时本通道实际调用 deepseek-chat
    model_map: dict[str, str] = {}
    tags: list[str] = []


class ProviderConfig(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    timeout: int = 30
    channels: list[ChannelConfig] = []


class LoadBalancingConfig(BaseModel):
    strategy: str = "weighted_round_robin"
    health_check_interval: int = 30
    cooldown_seconds: int = 60


class RateLimitingConfig(BaseModel):
    algorithm: str = "sliding_window"
    default_rpm: int = 60
    default_tpm: int = 100000
    per_key: bool = True


class StreamingConfig(BaseModel):
    """流式相关配置。

    first_token_timeout 只负责一件事：等"第一个 token"最多多少秒。
    超时就判定该通道失败并切下一个通道 —— 此时客户端一个字都还没收到，切换是安全的。
    （与 provider 的 timeout 是两个概念：timeout 是"整段话说多久"，这里只是"开不开口"。）
    """

    first_token_timeout: float = 5.0


class GatewayConfig:
    """全局配置单例"""

    _instance: Optional["GatewayConfig"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def load(self, config_dir: str | None = None):
        if self._loaded:
            return
        if config_dir is None:
            config_dir = str(Path(__file__).parent.parent / "config")

        # 加载提供商配置
        providers_path = os.path.join(config_dir, "providers.yaml")
        with open(providers_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        # 展开环境变量 ${VAR}
        self.providers: list[ProviderConfig] = []
        for p in raw.get("providers", []):
            p["api_key"] = self._expand_env(p.get("api_key", ""))
            self.providers.append(ProviderConfig(**p))

        self.model_aliases: dict[str, str] = raw.get("model_aliases", {})
        self.load_balancing = LoadBalancingConfig(**raw.get("load_balancing", {}))
        self.rate_limiting = RateLimitingConfig(**raw.get("rate_limiting", {}))
        self.streaming = StreamingConfig(**raw.get("streaming", {}))

        # 加载提示词配置
        prompts_path = os.path.join(config_dir, "prompts.yaml")
        with open(prompts_path, "r", encoding="utf-8") as f:
            self.prompts_raw = yaml.safe_load(f)

        self._loaded = True

    @staticmethod
    def _expand_env(val: str) -> str:
        """展开 ${VAR} 格式的环境变量"""
        if val and val.startswith("${") and val.endswith("}"):
            env_key = val[2:-1]
            return os.environ.get(env_key, "")
        return val

    def get_provider(self, name: str) -> ProviderConfig | None:
        for p in self.providers:
            if p.name == name:
                return p
        return None

    def find_channels_for_model(self, model: str) -> list[tuple[ProviderConfig, ChannelConfig]]:
        """查找支持指定模型的所有通道，按优先级排序

        支持两种匹配:
        1. 通道 models 列表直接包含该模型名
        2. 通道 model_map 中存在该虚拟模型名 (跨提供商故障转移用)
        """
        # 先解析别名
        resolved = self.model_aliases.get(model, model)
        results = []
        for p in self.providers:
            for ch in p.channels:
                if resolved in ch.models or resolved in ch.model_map:
                    results.append((p, ch))
        # 按优先级排序（priority 小的优先）
        results.sort(key=lambda x: x[1].priority)
        return results

    @property
    def db_path(self) -> str:
        return str(Path(__file__).parent.parent / "data" / "gateway.db")


# 全局配置实例
config = GatewayConfig()

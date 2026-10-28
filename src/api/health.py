"""
健康检查与状态端点
提供网关运行状态、通道健康、断路器状态等信息
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.gateway.load_balancer import load_balancer
from src.gateway.circuit_breaker import breaker_manager
from src.cache.response_cache import response_cache
from src.audit.logger import audit_logger
from src.config import config

router = APIRouter()


@router.get("/health")
async def health_check():
    """基本健康检查"""
    return {"status": "ok", "service": "llm-gateway"}


@router.get("/v1/models")
async def list_models():
    """列出所有可用模型"""
    models = set()
    for p in config.providers:
        for ch in p.channels:
            for m in ch.models:
                models.add(m)
    # 加上别名
    for alias in config.model_aliases:
        models.add(alias)

    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "owned_by": "llm-gateway"}
            for m in sorted(models)
        ],
    }


@router.get("/v1/status")
async def gateway_status():
    """网关完整状态"""
    return JSONResponse({
        "service": "llm-gateway",
        "providers": [
            {
                "name": p.name,
                "base_url": p.base_url,
                "channels": [
                    {"id": ch.id, "priority": ch.priority, "weight": ch.weight,
                     "models": ch.models, "tags": ch.tags}
                    for ch in p.channels
                ],
            }
            for p in config.providers
        ],
        "model_aliases": config.model_aliases,
        "load_balancing": {
            "strategy": config.load_balancing.strategy,
            "health_check_interval": config.load_balancing.health_check_interval,
        },
        "rate_limiting": {
            "algorithm": config.rate_limiting.algorithm,
            "default_rpm": config.rate_limiting.default_rpm,
        },
        "channel_health": load_balancer.all_health_status(),
        "circuit_breakers": breaker_manager.all_status(),
        "cache_stats": response_cache.stats(),
    })

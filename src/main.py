"""
LLM Gateway - 主应用入口
大模型智能网关系统

功能模块:
  1. OpenAI 兼容 API 代理 (/v1/chat/completions)
  2. 智能负载均衡 (加权轮询/最低延迟/轮询/随机)
  3. 模型路由与故障转移 (断路器状态机)
  4. 提示词版本化管理与 A/B 测试
  5. 防篡改审计哈希链 (命令审计)
  6. 限流 (滑动窗口)
  7. 响应缓存
  8. Web 监控仪表盘

启动: python src/main.py
"""

import os
import sys
import logging
import uvicorn
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# 确保可以 import src.xxx
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import config
from src.gateway.provider import provider_manager
from src.prompt.registry import prompt_registry
from src.audit.logger import audit_logger
from src.api import completions, health, prompts, audit, usage

# ── 日志配置 ──────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("llm-gateway")


# ── 生命周期管理 ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动与关闭钩子"""
    # ── 启动 ──
    logger.info("=" * 60)
    logger.info("  LLM Gateway — 大模型智能网关  Starting...")
    logger.info("=" * 60)

    # 加载配置
    config.load()

    # 初始化提供商适配器
    provider_manager.init()

    # 加载提示词注册表
    prompt_registry.load()

    # 初始化审计日志 (异步写入)
    audit_logger.init()

    logger.info("All modules initialized:")
    logger.info(f"  - Providers:    {len(config.providers)}")
    logger.info(f"  - Prompts:      {len(prompt_registry.list_all())}")
    logger.info(f"  - DB:           {config.db_path}")
    logger.info(f"  - LB Strategy:  {config.load_balancing.strategy}")
    logger.info(f"  - Rate Limit:   {config.rate_limiting.default_rpm} rpm")
    logger.info("=" * 60)

    yield

    # ── 关闭 ──
    logger.info("Shutting down LLM Gateway...")
    await provider_manager.close_all()
    logger.info("Goodbye.")


# ── 创建 FastAPI 应用 ─────────────────────────────────────────

app = FastAPI(
    title="LLM Gateway",
    description="大模型智能网关 — 负载均衡 / 调度 / 提示词管理 / 命令审计",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件 (仪表盘)
static_dir = str(Path(__file__).parent.parent / "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# 注册 API 路由
app.include_router(health.router, tags=["health"])
app.include_router(completions.router_api, tags=["completions"])
app.include_router(prompts.router, tags=["prompts"])
app.include_router(audit.router, tags=["audit"])
app.include_router(usage.router, tags=["usage"])


@app.get("/")
async def index():
    """重定向到仪表盘"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/static/dashboard.html")


# ── 入口 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    host = os.environ.get("GATEWAY_HOST", "0.0.0.0")
    port = int(os.environ.get("GATEWAY_PORT", "8080"))
    logger.info(f"Starting server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)

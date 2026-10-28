"""
Pydantic 数据模型定义
OpenAI 兼容的请求/响应模式
"""

from pydantic import BaseModel, Field
from typing import Optional, Any
from enum import Enum


# ── OpenAI 兼容请求/响应 ──────────────────────────────────────

class Message(BaseModel):
    role: str = "user"
    content: str = ""


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[Message]
    temperature: Optional[float] = 1.0
    max_tokens: Optional[int] = None
    top_p: Optional[float] = 1.0
    stream: Optional[bool] = False
    # 透传其他参数
    prompt_id: Optional[str] = None       # 使用注册的提示词模板
    prompt_vars: Optional[dict] = None     # 提示词模板变量
    metadata: Optional[dict] = None       # 附加元数据


class Choice(BaseModel):
    index: int = 0
    message: Message
    finish_reason: str = "stop"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage = Usage()


# ── 健康状态 ──────────────────────────────────────────────────

class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


# ── 审计记录 ──────────────────────────────────────────────────

class AuditEntry(BaseModel):
    trace_id: str
    timestamp: str
    api_key: str
    model: str
    provider: str
    channel_id: str
    prompt_id: Optional[str] = None
    prompt_version: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    status_code: int = 200
    error: Optional[str] = None
    request_hash: str = ""
    prev_hash: str = ""
    curr_hash: str = ""


# ── 提示词管理 ────────────────────────────────────────────────

class PromptVariable(BaseModel):
    name: str
    type: str = "string"       # string | number | select
    required: bool = False
    default: Any = None
    options: Optional[list] = None


class PromptVersion(BaseModel):
    version: str
    weight: int = 100
    template: str
    variables: list[PromptVariable] = []


class PromptEntry(BaseModel):
    id: str
    name: str
    description: str = ""
    active_version: str = ""
    versions: list[PromptVersion] = []

"""
审计日志查询 API
提供审计日志查询、哈希链验证、统计信息等接口
"""

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import JSONResponse

from src.audit.logger import audit_logger

router = APIRouter()


@router.get("/v1/audit/logs")
async def query_audit_logs(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    model: str = Query(None),
    provider: str = Query(None),
    status_code: int = Query(None),
):
    """查询审计日志"""
    if audit_logger.store is None:
        raise HTTPException(status_code=503, detail="Audit store not initialized")
    logs = audit_logger.store.query(
        limit=limit, offset=offset,
        model=model, provider=provider, status_code=status_code,
    )
    return JSONResponse(content={"logs": logs, "count": len(logs)})


@router.get("/v1/audit/verify")
async def verify_audit_chain():
    """验证审计哈希链完整性"""
    if audit_logger.store is None:
        raise HTTPException(status_code=503, detail="Audit store not initialized")
    result = audit_logger.store.verify_chain()
    return JSONResponse(content=result)


@router.get("/v1/audit/stats")
async def audit_stats():
    """审计统计信息"""
    if audit_logger.store is None:
        raise HTTPException(status_code=503, detail="Audit store not initialized")
    stats = audit_logger.store.stats()
    return JSONResponse(content=stats)

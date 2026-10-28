"""/v1/usage — 成本聚合端点

思路：
  1. token 数量从审计表聚合（SQL，见 AuditStore.usage_summary）
  2. 单价从 config/pricing.yaml 读（元 / 百万 token）
  3. cost = input_tokens/1e6 * 输入单价 + output_tokens/1e6 * 输出单价

一个诚实的取舍：
  · group_by=model 时成本**精确**（provider+model 才能定位单价）
  · group_by=provider / day 时单价信息不足，用 pricing.yaml 的 fallback 价估算，
    并在返回里标 "cost_estimated": true —— 生产上也是这样，成本归因最细的维度是 provider×model。
"""

from pathlib import Path

import yaml
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from src.audit.logger import audit_logger

router = APIRouter()

PRICING_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "pricing.yaml"
_pricing: dict | None = None


def load_pricing() -> dict:
    global _pricing
    if _pricing is None:
        try:
            with open(PRICING_PATH, "r", encoding="utf-8") as f:
                _pricing = yaml.safe_load(f) or {}
        except FileNotFoundError:
            _pricing = {}
    return _pricing


def unit_price(provider: str | None, model: str | None) -> tuple[float, float]:
    """返回 (输入单价, 输出单价)，单位：元/百万 token"""
    data = load_pricing()
    fallback = data.get("fallback", {}) or {}
    table = data.get("pricing", {}) or {}
    item = ((table.get(provider or "") or {}).get(model or "")) or {}
    return (
        float(item.get("input", fallback.get("input", 2.0))),
        float(item.get("output", fallback.get("output", 8.0))),
    )


def cost_of(provider: str | None, model: str | None, in_tok: int, out_tok: int) -> float:
    pin, pout = unit_price(provider, model)
    return in_tok / 1_000_000 * pin + out_tok / 1_000_000 * pout


@router.get("/v1/usage")
async def usage(
    group_by: str = Query("provider", pattern="^(provider|model|day)$"),
    since: str | None = Query(None, description="只统计该时间之后的记录，如 2026-10-26"),
):
    store = audit_logger.store
    if store is None:
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "store_not_ready", "message": "审计存储尚未初始化"}},
        )

    try:
        rows = store.usage_summary(group_by=group_by, since=since)
    except NotImplementedError as e:
        # usage_summary 还没实现时，给一个明确的 501，而不是 500
        return JSONResponse(
            status_code=501,
            content={"error": {"code": "not_implemented", "message": str(e)}},
        )

    exact = group_by == "model"
    breakdown = []
    total_cost = 0.0
    for r in rows:
        provider = r.get("provider")
        model = r.get("model")
        in_tok = int(r.get("input_tokens") or 0)
        out_tok = int(r.get("output_tokens") or 0)
        cost = cost_of(provider, model, in_tok, out_tok)
        total_cost += cost
        breakdown.append(
            {
                "key": r.get("key"),
                "requests": int(r.get("requests") or 0),
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "total_tokens": int(r.get("total_tokens") or 0),
                "avg_latency_ms": round(float(r.get("avg_latency_ms") or 0), 2),
                "cost": round(cost, 6),
            }
        )

    return {
        "group_by": group_by,
        "since": since,
        "cost_estimated": not exact,     # provider/day 视图用兜底价，属估算
        "total_requests": sum(x["requests"] for x in breakdown),
        "total_tokens": sum(x["total_tokens"] for x in breakdown),
        "total_cost": round(total_cost, 6),
        "currency": "CNY",
        "breakdown": sorted(breakdown, key=lambda x: x["total_tokens"], reverse=True),
    }

"""
提示词管理 API
提供提示词列表、详情、Dry-run 预览等接口
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from src.prompt.registry import prompt_registry

router = APIRouter()


@router.get("/v1/prompts")
async def list_prompts():
    """列出所有注册的提示词"""
    return JSONResponse(content=prompt_registry.to_dict())


@router.get("/v1/prompts/{prompt_id}")
async def get_prompt(prompt_id: str):
    """获取单个提示词详情"""
    entry = prompt_registry.get(prompt_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Prompt not found: {prompt_id}")
    return JSONResponse(content={
        "id": entry.id,
        "name": entry.name,
        "description": entry.description,
        "active_version": entry.active_version,
        "versions": [
            {
                "version": v.version,
                "weight": v.weight,
                "template": v.template,
                "variables": [var.model_dump() for var in v.variables],
            }
            for v in entry.versions
        ],
    })


@router.post("/v1/prompts/{prompt_id}/dry-run")
async def dry_run_prompt(prompt_id: str, request: Request):
    """
    Dry-run: 渲染提示词模板变量，不调用模型
    请求体: {"variables": {"var_name": "value"}, "version": "v1"}
    """
    body = await request.json()
    variables = body.get("variables", {})
    version = body.get("version")

    try:
        result = prompt_registry.dry_run(prompt_id, variables, version)
        return JSONResponse(content=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

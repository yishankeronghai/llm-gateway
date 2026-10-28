"""
提示词注册表 (Prompt Registry)
版本化提示词管理 + A/B 测试 + 模板变量渲染

参考: LLMGov 的 prompt_registry.py — 版本化 A/B 提示词路由注册表
参考: AIGC Gateway 的 Action/Template 两层抽象
"""
from __future__ import annotations

import random
import hashlib
import logging
from typing import Optional

import jinja2

from src.config import config
from src.models.schemas import PromptEntry, PromptVersion, PromptVariable

logger = logging.getLogger("prompt.registry")


class PromptRegistry:
    """
    提示词注册表
    - 从 YAML 加载版本化提示词
    - 支持按权重进行 A/B 测试路由
    - 支持模板变量渲染 (Jinja2)
    """

    def __init__(self):
        self._prompts: dict[str, PromptEntry] = {}
        self._jinja_env = jinja2.Environment(
            loader=jinja2.BaseLoader(),
            autoescape=False,
            undefined=jinja2.Undefined,
        )

    def load(self):
        """从配置加载提示词"""
        raw = config.prompts_raw
        for p in raw.get("prompts", []):
            entry = PromptEntry(**p)
            self._prompts[entry.id] = entry
            logger.info(
                f"Prompt loaded: {entry.id} "
                f"(active={entry.active_version}, "
                f"versions={len(entry.versions)})"
            )

    def get(self, prompt_id: str) -> Optional[PromptEntry]:
        return self._prompts.get(prompt_id)

    def list_all(self) -> list[PromptEntry]:
        return list(self._prompts.values())

    def resolve(
        self,
        prompt_id: str,
        variables: dict | None = None,
        version: str | None = None,
    ) -> tuple[str, str, str]:
        """
        解析提示词模板
        返回: (渲染后的系统提示词, prompt_id, 选中的版本号)

        如果指定了 version，使用该版本；否则按权重随机选择
        """
        entry = self._prompts.get(prompt_id)
        if entry is None:
            raise ValueError(f"Prompt not found: {prompt_id}")

        # 选择版本
        if version:
            selected = self._find_version(entry, version)
        else:
            selected = self._weighted_select(entry)

        if selected is None:
            raise ValueError(f"No version found for prompt: {prompt_id}")

        # 渲染模板变量
        rendered = self._render(selected.template, variables or {})

        return rendered, prompt_id, selected.version

    def _find_version(self, entry: PromptEntry, version: str) -> Optional[PromptVersion]:
        for v in entry.versions:
            if v.version == version:
                return v
        return None

    def _weighted_select(self, entry: PromptEntry) -> Optional[PromptVersion]:
        """
        按权重随机选择版本 (A/B 测试)
        先尝试用 active_version，再做加权随机
        """
        if not entry.versions:
            return None

        # 如果只有一个版本，直接返回
        if len(entry.versions) == 1:
            return entry.versions[0]

        # 加权随机选择
        total_weight = sum(v.weight for v in entry.versions)
        if total_weight == 0:
            return entry.versions[0]

        r = random.uniform(0, total_weight)
        cumulative = 0
        for v in entry.versions:
            cumulative += v.weight
            if r <= cumulative:
                logger.debug(
                    f"Prompt {entry.id}: selected version {v.version} "
                    f"(weight={v.weight}/{total_weight})"
                )
                return v

        return entry.versions[-1]

    def _render(self, template_str: str, variables: dict) -> str:
        """
        渲染模板变量
        支持两种语法:
        - {{var}}          → 替换为 variables[var]
        - {{var|default}}  → 如果 var 未提供，使用 default 作为默认值
        """
        import re

        def replace_var(match):
            expr = match.group(1).strip()
            if "|" in expr:
                parts = expr.split("|", 1)
                var_name = parts[0].strip()
                default_val = parts[1].strip()
                return str(variables.get(var_name, default_val))
            else:
                var_name = expr
                if var_name in variables:
                    return str(variables[var_name])
                # 未提供且无默认值 → 保留占位符
                return match.group(0)

        pattern = r"\{\{([^}]+)\}\}"
        return re.sub(pattern, replace_var, template_str)

    def dry_run(
        self,
        prompt_id: str,
        variables: dict | None = None,
        version: str | None = None,
    ) -> dict:
        """
        Dry-run: 仅渲染变量不调模型
        用于预览渲染后的 prompt
        """
        rendered, pid, ver = self.resolve(prompt_id, variables, version)
        entry = self._prompts.get(pid)
        return {
            "prompt_id": pid,
            "version": ver,
            "rendered_template": rendered,
            "variables": variables or {},
            "available_versions": [v.version for v in entry.versions] if entry else [],
        }

    def to_dict(self) -> list[dict]:
        result = []
        for entry in self._prompts.values():
            result.append({
                "id": entry.id,
                "name": entry.name,
                "description": entry.description,
                "active_version": entry.active_version,
                "versions": [
                    {
                        "version": v.version,
                        "weight": v.weight,
                        "template": v.template[:100] + "..." if len(v.template) > 100 else v.template,
                        "variables": [var.model_dump() for var in v.variables],
                    }
                    for v in entry.versions
                ],
            })
        return result


# 全局实例
prompt_registry = PromptRegistry()

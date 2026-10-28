"""
实验 3: Pydantic 配置校验 fail-fast 验证
========================================
用法: python experiments/exp3_validate.py <config_dir>

对应 docs/STUDY_PLAN.md 第 1 周实验 3。
验证三种配置错误在 "启动时" 还是 "运行时" 暴露:

  v1  weight: "abc"   → 类型错误, 应启动即失败
  v2  weight: "100"   → 数字字符串, Pydantic 宽松模式会安全转成 int (对比"静默错误")
  v3  删除 id 字段     → 必填缺失, 应启动即失败

对照实验: 用普通 dict 加载同样的配置, 会发现"什么都不会发生"(错误被藏起来)。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.config import config

cfg_dir = sys.argv[1] if len(sys.argv) > 1 else None

# ── 对照组: 普通 dict 世界 (不做任何校验) ──
import yaml

raw = yaml.safe_load(open(Path(cfg_dir) / "providers.yaml", encoding="utf-8"))
raw_weight = raw["providers"][0]["channels"][0].get("weight")
print(f"  [普通 dict 加载] 一生不响地读进来: weight={raw_weight!r} "
      f"({type(raw_weight).__name__})  ← 没有任何报错")

# ── 实验组: Pydantic 校验 ──
try:
    config.load(config_dir=cfg_dir)
    ch = config.providers[0].channels[0]
    print(f"  [Pydantic 加载]  ✅ 校验通过, 字段被规整为: "
          f"id={ch.id!r}, weight={ch.weight!r} ({type(ch.weight).__name__})")
except Exception as e:
    msg = str(e).splitlines()
    print(f"  [Pydantic 加载]  ❌ 启动即失败, 错误信息:")
    for line in msg[:6]:
        print(f"       {line}")

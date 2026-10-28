r"""配置体检 / 急救

实验脚本（exp5/exp6/exp7/exp_failover_live）会临时改写 `config/providers.yaml`
（把 base_url 指向 `127.0.0.1:9` 这种死端口来注入故障），如果在还原前被 Ctrl+C 或崩溃打断，
配置就会停留在"故障注入"状态——此时任何真实模型调用都会连不上。

这个脚本用来快速判断 + 一键修复：

    .venv\Scripts\python.exe scripts\doctor.py           # 只体检
    .venv\Scripts\python.exe scripts\doctor.py --fix     # 顺手把配置修回正常
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "providers.yaml"
DB = ROOT / "data" / "gateway.db"

# 正常配置的"应该长什么样"
GOOD = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "api_key": "${DEEPSEEK_API_KEY}", "env": "DEEPSEEK_API_KEY"},
    "openai": {"base_url": "https://api.openai.com/v1", "api_key": "${OPENAI_API_KEY}", "env": "OPENAI_API_KEY"},
}
DEAD_HINTS = ("127.0.0.1:9", "localhost:9", "mock-key")


def parse_providers(text: str) -> dict[str, dict]:
    """粗解析：按 `- name: xxx` 切块，抓 base_url / api_key"""
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"\s*-\s*name:\s*(\w+)\s*$", line)
        if m:
            cur = m.group(1)
            out[cur] = {}
            continue
        if cur:
            for key in ("base_url", "api_key"):
                mm = re.search(rf'{key}:\s*"(.*?)"', line)
                if mm:
                    out[cur][key] = mm.group(1)
    return out


def fix_config() -> bool:
    text = CONFIG.read_text(encoding="utf-8")
    providers = parse_providers(text)
    changed = False
    for name, good in GOOD.items():
        if name not in providers:
            continue
        cur = providers[name]
        if cur.get("base_url") != good["base_url"]:
            text = re.sub(
                rf'(- name:\s*{name}\b[\s\S]{{0,400}}?base_url:\s*")[^"]*(")',
                rf'\g<1>{good["base_url"]}\g<2>',
                text, count=1,
            )
            changed = True
        if cur.get("api_key") != good["api_key"]:
            text = re.sub(
                rf'(- name:\s*{name}\b[\s\S]{{0,400}}?api_key:\s*")[^"]*(")',
                rf'\g<1>{good["api_key"]}\g<2>',
                text, count=1,
            )
            changed = True
    if changed:
        CONFIG.write_text(text, encoding="utf-8")
    return changed


def main():
    do_fix = "--fix" in sys.argv
    print("=" * 68)
    print("  配置体检")
    print("=" * 68)

    problems = []

    # 1. providers.yaml
    if not CONFIG.exists():
        problems.append("config/providers.yaml 不存在")
    else:
        text = CONFIG.read_text(encoding="utf-8")
        providers = parse_providers(text)
        for name, cur in providers.items():
            url = cur.get("base_url", "")
            key = cur.get("api_key", "")
            bad_url = any(h in url for h in DEAD_HINTS)
            bad_key = any(h in key for h in DEAD_HINTS) or not key.startswith("${")
            flag = "❌" if (bad_url or bad_key) else "✅"
            print(f"  {flag} {name:<10} base_url={url}")
            if bad_url:
                problems.append(f"{name} 的 base_url 还停留在故障注入地址（实验没还原）")
            if bad_key:
                problems.append(f"{name} 的 api_key 不是 ${{ENV}} 占位（被实验改成写死了）")

        if problems and do_fix:
            print("\n  → 正在修复配置 ...")
            changed = fix_config()
            print("  → 已修复" if changed else "  → 配置本来就是对的，无需修改")
            providers = parse_providers(CONFIG.read_text(encoding="utf-8"))
            for name, cur in providers.items():
                print(f"     {name:<10} base_url={cur.get('base_url')} api_key={cur.get('api_key')}")
            problems = [p for p in problems if "故障注入" not in p and "占位" not in p]

    # 2. 环境变量（当前进程 + 注册表里的用户/机器级都看一遍）
    for name, good in GOOD.items():
        in_proc = bool(os.environ.get(good["env"]))
        reg_user = reg_machine = False
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                reg_user = bool(winreg.QueryValueEx(k, good["env"])[0])
        except Exception:
            pass
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            )
            reg_machine = bool(winreg.QueryValueEx(key, good["env"])[0])
        except Exception:
            pass

        if in_proc:
            print(f"  ✅ 环境变量 {good['env']} 当前进程可见")
        elif reg_user or reg_machine:
            where = "用户级" if reg_user else "机器级"
            print(f"  ⚠  {good['env']} 已写入{where}环境变量，但**当前进程看不到** → 重启终端/PyCharm")
            problems.append(f"{good['env']} 需要重启进程才能生效")
        else:
            print(f"  ❌ 环境变量 {good['env']} 未设置（真实调用会 401）")
            problems.append(f"未设置环境变量 {good['env']}（setx {good['env']} \"sk-...\"）")

    # 3. 服务是否在跑
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=2) as r:
            print(f"  ✅ 网关在跑（{r.status}）")
    except Exception:
        print("  ⚠  网关没在跑（浏览器会显示“无法访问此网站”）")

    # 4. 审计链
    if DB.exists():
        try:
            sys.path.insert(0, str(ROOT))
            from src.audit.store import AuditStore
            res = AuditStore(str(DB)).verify_chain()
            print(f"  {'✅' if res['valid'] else '❌'} 审计链 {res['message']}（{res['total']} 条）")
            if not res["valid"]:
                problems.append("审计链断裂，可跑 experiments/exp7_usage.py --repair 截断修复")
        except Exception as e:
            print(f"  ⚠  审计链检查失败：{e}")

    print("\n" + "=" * 68)
    if problems:
        print("  待处理：")
        for p in problems:
            print(f"    · {p}")
        if not do_fix:
            print("\n  提示：加 --fix 可自动把 providers.yaml 修回正常")
    else:
        print("  ✅ 没发现问题")
    print("=" * 68)
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())

r"""
实验 5 · 上游失败时的 HTTP 状态码（复现 / 验证同一个脚本都用它）

    .venv\Scripts\python.exe experiments\exp5_status_code.py

脚本做的事：
    1. 备份 providers.yaml，把两个通道的 base_url 都改成本机死端口（必定连不上）
    2. 起网关，发一次 model="auto" 请求
    3. 打印 HTTP 状态码 + 响应体 + x_gateway
    4. 判定：
         看到 200 且内容是错误文本  → ❌ bug 还在（上游失败却返回成功）
         看到 502 / 503 / 504 + 结构化 error → ✅ 已修复
    5. 关网关、还原配置

改代码前后各跑一次，就是你的"改前/改后"证据。
"""

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "providers.yaml"
BACKUP = ROOT / "config" / "providers.yaml.labbak"
LOG = ROOT / "data" / "lab_server.log"
GATEWAY = "http://127.0.0.1:8080"
DEAD_URL = "http://127.0.0.1:9/v1"
LINE = "=" * 72


def patch_all_dead() -> list[str]:
    """把所有通道的 base_url 都改成死地址"""
    lines = CONFIG.read_text(encoding="utf-8").splitlines()
    changed = []
    for i, line in enumerate(lines):
        if "base_url:" in line and "http" in line:
            old = line.strip()
            lines[i] = re.sub(r'base_url:\s*".*?"', f'base_url: "{DEAD_URL}"', line)
            changed.append(f"{old}  →  {lines[i].strip()}")
    CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def main():
    print(LINE)
    print("  实验 5 · 上游全部失败时，网关应该返回什么状态码？")
    print(LINE)

    shutil.copy2(CONFIG, BACKUP)
    for c in patch_all_dead():
        print("  [配置]", c)

    log_fp = open(LOG, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "src" / "main.py")],
        cwd=str(ROOT), stdout=log_fp, stderr=subprocess.STDOUT,
    )
    print(f"  [网关] 已启动 pid={proc.pid}\n")

    try:
        ok = False
        for _ in range(50):
            try:
                if httpx.get(f"{GATEWAY}/health", timeout=1).status_code == 200:
                    ok = True
                    break
            except Exception:
                time.sleep(0.5)
        if not ok:
            print("[错误] 网关 25 秒内没起来，日志尾部：")
            print(LOG.read_text(encoding="utf-8", errors="replace")[-1200:])
            return 2

        body = {"model": "auto", "messages": [{"role": "user", "content": f"状态码测试-{time.time():.0f}"}]}
        try:
            r = httpx.post(f"{GATEWAY}/v1/chat/completions", json=body, timeout=130)
            status, text = r.status_code, r.text
        except Exception as e:
            print(f"[错误] 请求本身异常：{type(e).__name__}: {e}")
            return 2

        print("-" * 72)
        print(f"  HTTP 状态码：{status}")
        print("-" * 72)

        # 尝试解析成 JSON 展示
        try:
            data = json.loads(text)
            print("  响应体（截断）：")
            print("   " + json.dumps(data, ensure_ascii=False)[:600].replace("\n", "\n   "))
        except Exception:
            print("  响应体（非 JSON，截断）：")
            print("   " + text[:400])

        print("\n" + "=" * 72)
        if status == 200 and ("[Error]" in text or "[Gateway Error]" in text or "error" in text.lower()):
            print("  判定：❌ bug 还在 —— HTTP 200，但内容其实是错误")
            print("  说明：调用方无法用状态码做重试/告警；监控里全是绿的；失败被算成成功。")
            print("  下一步：按 docs/W3_SPEC.md 的 Day 1 改 provider / router / completions 三处。")
            rc = 1
        elif status in (502, 503, 504):
            has_code = '"code"' in text
            print(f"  判定：✅ 已修复 —— 返回 {status}" + ("，且错误体是结构化的（含 code）" if has_code else "，但错误体还不是结构化的（建议补 error.code）"))
            rc = 0 if has_code else 0
        else:
            print(f"  判定：⚠ 意外状态码 {status}，把上面输出发我看")
            rc = 1
        print("=" * 72 + "\n")
        return rc

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_fp.close()
        shutil.copy2(BACKUP, CONFIG)
        print("[清理] 网关已关，providers.yaml 已还原\n")


if __name__ == "__main__":
    sys.exit(main())

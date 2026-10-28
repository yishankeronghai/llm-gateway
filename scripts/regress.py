r"""一键回归：语法编译 + 故障转移单测 + 端到端测试

用法（在项目根目录）：
    .venv\Scripts\python.exe scripts\regress.py
    .venv\Scripts\python.exe scripts\regress.py --skip-e2e      # 不想起服务时

目标：**只输出 3 行摘要**，不打整屏日志。每次改完代码跑一次。
退出码：0 = 全绿，1 = 有失败项。
"""

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)
LOG = ROOT / "data" / "regress.log"
BASE = "http://127.0.0.1:8080"

summary: list[str] = []


def run_to_file(cmd: list[str]) -> tuple[int, str]:
    """跑子进程，输出写文件（不用管道，Windows 沙箱下更稳），返回 (退出码, 文本)"""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "w", encoding="utf-8", errors="replace") as fp:
        proc = subprocess.run(cmd, cwd=str(ROOT), stdout=fp, stderr=subprocess.STDOUT, timeout=300)
    return proc.returncode, LOG.read_text(encoding="utf-8", errors="replace")


def step_compile():
    rc, text = run_to_file([str(PY), "-m", "compileall", "-q", str(ROOT / "src")])
    if rc == 0:
        summary.append("语法编译       PASS")
    else:
        tail = " / ".join([ln for ln in text.splitlines() if ln.strip()][-2:])
        summary.append(f"语法编译       FAIL  {tail}")


def step_failover():
    rc, text = run_to_file([str(PY), str(ROOT / "test_failover.py")])
    passed = text.count(">>> PASS")
    if rc == 0 and passed >= 3:
        summary.append(f"故障转移单测   PASS ({passed} 个场景)")
    else:
        tail = " / ".join([ln for ln in text.splitlines() if ln.strip()][-2:])
        summary.append(f"故障转移单测   FAIL ({passed} 个场景通过)  {tail}")


def wait_health(seconds: int = 20) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def step_e2e():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    server_log = open(ROOT / "data" / "regress_server.log", "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(PY), str(ROOT / "src" / "main.py")],
        cwd=str(ROOT), stdout=server_log, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_health():
            summary.append("端到端测试     FAIL (服务 20 秒内没起来)")
            return
        rc, text = run_to_file([str(PY), str(ROOT / "tests" / "test_gateway.py")])
        p, f = text.count("[PASS]"), text.count("[FAIL]")
        if f == 0 and p > 0:
            summary.append(f"端到端测试     PASS ({p} 项通过)")
        else:
            names = " / ".join([ln.strip() for ln in text.splitlines() if "[FAIL]" in ln][:2])
            summary.append(f"端到端测试     FAIL ({p} 通过 / {f} 失败)  {names}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        server_log.close()


def main():
    skip_e2e = "--skip-e2e" in sys.argv
    print("跑回归中…（输出细节写在 data\\regress.log，这里只给摘要）")
    step_compile()
    step_failover()
    if skip_e2e:
        summary.append("端到端测试     SKIP")
    else:
        step_e2e()

    print("\n== 回归摘要 ==")
    for line in summary:
        print("  " + line)
    failed = any("FAIL" in s for s in summary)
    print("\n  → " + ("有失败项，先修再继续。" if failed else "全绿。"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

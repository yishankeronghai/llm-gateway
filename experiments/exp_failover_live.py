r"""
实验 2 · 一键故障注入（带 mock 备用通道）
把主通道(deepseek)打成死地址，备用通道(openai)指向本机 mock，
连发 6 次 model="auto"，观察：主通道失败 → 自动切备用 → 5 次后熔断 → 第 6 次跳过主通道。

    .venv\Scripts\python.exe experiments\exp_failover_live.py

脚本会自己做完全部脏活：备份/改配置、起 mock 上流、起网关、发请求、打印证据、
最后关服务并还原配置。你只需要看输出。
"""

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
DEAD_URL = "http://127.0.0.1:9/v1"          # 9 号端口无人监听 → 连接必失败
MOCK_PORT = 8099
MOCK_URL = f"http://127.0.0.1:{MOCK_PORT}/v1"
LINE = "=" * 76


# ─────────────────────────── mock 上游 ───────────────────────────
class MockUpstream(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):  # noqa: N802
        MockUpstream.calls += 1
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({
            "id": f"mock-{MockUpstream.calls}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "mock-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": f"[mock 备用通道] 第 {MockUpstream.calls} 次被调用，主通道已故障"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 8, "completion_tokens": 12, "total_tokens": 20},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# ─────────────────────────── 配置改写 ───────────────────────────
def patch_config():
    """deepseek → 死地址；openai → 本机 mock"""
    lines = CONFIG.read_text(encoding="utf-8").splitlines()
    provider = None
    changed = []
    for i, line in enumerate(lines):
        m = re.match(r"\s*-\s*name:\s*(\w+)\s*$", line)
        if m:
            provider = m.group(1)
            continue
        if provider == "deepseek" and "base_url:" in line:
            lines[i] = re.sub(r'base_url:\s*".*?"', f'base_url: "{DEAD_URL}"', line)
            changed.append(f"deepseek.base_url → {DEAD_URL}")
            continue
        if provider == "openai" and "base_url:" in line:
            lines[i] = re.sub(r'base_url:\s*".*?"', f'base_url: "{MOCK_URL}"', line)
            changed.append(f"openai.base_url → {MOCK_URL}")
            continue
        if provider == "openai" and "api_key:" in line:
            lines[i] = re.sub(r'api_key:\s*".*?"', 'api_key: "mock-key"', line)
            changed.append('openai.api_key → "mock-key"')
            continue
    CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def restore_config():
    if BACKUP.exists():
        shutil.copy2(BACKUP, CONFIG)


# ─────────────────────────── 主流程 ───────────────────────────
def wait_health(seconds: int = 25) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if httpx.get(f"{GATEWAY}/health", timeout=2).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def breakers() -> dict:
    try:
        data = httpx.get(f"{GATEWAY}/v1/status", timeout=5).json()
    except Exception:
        return {}
    return {b["name"]: b["state"] for b in data.get("circuit_breakers", [])}


def main():
    print(LINE)
    print("  实验 2 · 主通道故障 → 自动切备用 → 5 次熔断 → 第 6 次跳过主通道")
    print(LINE)

    shutil.copy2(CONFIG, BACKUP)
    for c in patch_config():
        print("  [配置]", c)

    mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockUpstream)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    print(f"  [mock] 备用上游已起在 {MOCK_URL}")

    log_fp = open(LOG, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "src" / "main.py")],
        cwd=str(ROOT), stdout=log_fp, stderr=subprocess.STDOUT,
    )
    print(f"  [网关] 已启动 pid={proc.pid}，日志 data/lab_server.log")

    ok = False
    try:
        if not wait_health():
            print("\n[错误] 网关没起来，日志尾部：")
            print(LOG.read_text(encoding="utf-8", errors="replace")[-1500:])
            return 2

        print("\n" + "-" * 76)
        print(f"{'次数':<5}{'HTTP':<6}{'attempted_channels':<44}{'deepseek':<12}{'openai':<10}{'回复来源'}")
        print("-" * 76)

        rows = []
        for i in range(1, 7):
            # 每次 message 不同，避免命中响应缓存
            body = {"model": "auto", "messages": [{"role": "user", "content": f"测试第{i}次"}]}
            r = httpx.post(f"{GATEWAY}/v1/chat/completions", json=body, timeout=130)
            gw = r.json().get("x_gateway", {}) if r.status_code == 200 else {}
            attempted = gw.get("attempted_channels")
            content = (r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                       if r.status_code == 200 else "")
            b = breakers()
            rows.append((i, r.status_code, attempted, b.get("deepseek-primary"), b.get("openai-primary")))
            print(
                f"{i:<5}{r.status_code:<6}{str(attempted):<44}"
                f"{str(b.get('deepseek-primary')):<12}{str(b.get('openai-primary')):<10}"
                f"{'mock 备用' if 'mock' in content else (content[:20] or '-')}"
            )

        print("\n" + "-" * 76)
        print("  服务端日志证据（关键行）：")
        text = LOG.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if any(k in line for k in ("Routing to", "trying next channel", "Circuit", "skipping", "model map")):
                print("    " + line.strip()[:140])

        # 判定
        first_five_ok = all(
            r[2] == ["deepseek-primary", "openai-primary"] for r in rows[:5]
        )
        sixth_ok = rows[5][2] == ["openai-primary"]
        breaker_ok = rows[4][3] == "open" and rows[5][3] == "open"
        ok = first_five_ok and sixth_ok and breaker_ok and all(r[1] == 200 for r in rows)

        print("\n" + LINE)
        print(f"  ① 前 5 次都先试主通道再切备用     : {'✅' if first_five_ok else '❌'}")
        print(f"  ② 第 5 次后 deepseek 熔断开(open) : {'✅' if breaker_ok else '❌'}")
        print(f"  ③ 第 6 次只剩 [openai-primary]    : {'✅' if sixth_ok else '❌'}")
        print(f"  ④ 6 次全部 HTTP 200（用户无感）   : {'✅' if all(r[1] == 200 for r in rows) else '❌'}")
        print(f"  → 实验 2：{'✅ 通过' if ok else '⚠ 未完全符合，把上面输出发我'}")
        print(LINE + "\n")
        return 0 if ok else 1

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_fp.close()
        mock.shutdown()
        restore_config()
        print("[清理] 网关已关、mock 已关、providers.yaml 已还原\n")


if __name__ == "__main__":
    sys.exit(main())

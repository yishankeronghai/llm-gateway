r"""
实验 6 · SSE 流式 + 首 token 超时切换（验收脚本）

    .venv\Scripts\python.exe experiments\exp6_stream.py

它会自动做两轮验证：
   A. 主通道打坏、备用通道换成"流式 mock 上游"
      → 期望：连续收到多个 chunk + [DONE]，并且响应头里能看到
        x-gateway-channel: openai-primary（说明切过通道）
        x-gateway-attempted: deepseek-primary,openai-primary
        x-gateway-ttft-ms: 首 token 延迟
   B. 两个通道都打坏
      → 期望：HTTP 502 + 结构化 error（因为一个 token 都没发出去，状态码还能用）

任何不符合的地方都会打印出来。跑完自动还原 config/providers.yaml。
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
DEAD_URL = "http://127.0.0.1:9/v1"
MOCK_PORT = 8099
MOCK_URL = f"http://127.0.0.1:{MOCK_PORT}/v1"
N_CHUNKS = 5
LINE = "=" * 72


# ─────────────────────── 流式 mock 上游 ───────────────────────
class MockSSE(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"       # 用关闭连接来界定响应体，天然支持流式

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for i in range(N_CHUNKS):
                payload = {
                    "id": "mock-stream",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"content": f"块{i}"}, "finish_reason": None}],
                }
                self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.08)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


# ─────────────────────── 配置改写 ───────────────────────
def patch(dead: bool, mock: bool):
    lines = CONFIG.read_text(encoding="utf-8").splitlines()
    provider = None
    for i, line in enumerate(lines):
        m = re.match(r"\s*-\s*name:\s*(\w+)\s*$", line)
        if m:
            provider = m.group(1)
            continue
        if provider == "deepseek" and "base_url:" in line:
            lines[i] = re.sub(r'base_url:\s*".*?"', f'base_url: "{DEAD_URL}"', line)
        elif provider == "openai" and "base_url:" in line:
            target = MOCK_URL if mock else DEAD_URL
            lines[i] = re.sub(r'base_url:\s*".*?"', f'base_url: "{target}"', line)
        elif provider == "openai" and "api_key:" in line:
            lines[i] = re.sub(r'api_key:\s*".*?"', 'api_key: "mock-key"', line)
    CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def wait_health(seconds: int = 20) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if httpx.get(f"{GATEWAY}/health", timeout=1).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


class Gateway:
    def __init__(self):
        self.proc = None
        self.log = None

    def __enter__(self):
        if LOG.exists():
            LOG.unlink()
        self.log = open(LOG, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "src" / "main.py")],
            cwd=str(ROOT), stdout=self.log, stderr=subprocess.STDOUT,
        )
        if not wait_health():
            raise SystemExit("[错误] 网关没起来，见 data/lab_server.log")
        return self

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        if self.log:
            self.log.close()


# ─────────────────────── 两轮验证 ───────────────────────
def case_stream() -> bool:
    print("\n" + "-" * 72)
    print("  场景 A · 主通道坏 → 流式请求应切到备用并完整输出")
    print("-" * 72)

    body = {"model": "auto", "messages": [{"role": "user", "content": f"流式测试-{time.time():.0f}"}], "stream": True}
    chunks, done, ttft = [], False, None
    started = time.time()
    broken = None
    with httpx.Client(timeout=30) as client:
        try:
            with client.stream("POST", f"{GATEWAY}/v1/chat/completions", json=body) as r:
                status = r.status_code
                headers = {k.lower(): v for k, v in r.headers.items()}
                if status != 200:
                    text = r.read().decode("utf-8", "replace")
                    print(f"  ✗ 状态码 {status}，响应体：{text[:200]}")
                    return False
                for line in r.iter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if ttft is None:
                        ttft = (time.time() - started) * 1000
                    if data == "[DONE]":
                        done = True
                        continue
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if "error" in obj:
                        print(f"  ✗ 流内错误事件：{obj['error']}")
                        return False
                    delta = obj.get("choices", [{}])[0].get("delta", {}).get("content")
                    if delta:
                        chunks.append(delta)
                        print(f"    ← {delta}   (+{(time.time()-started)*1000:.0f}ms)")
        except Exception as e:
            broken = f"{type(e).__name__}: {e}"

    print(f"\n  chunk 数 : {len(chunks)} （期望 {N_CHUNKS}）")
    print(f"  [DONE]   : {done}")
    print(f"  TTFT     : {ttft:.0f} ms" if ttft else "  TTFT     : 无")
    print(f"  响应头   : channel={headers.get('x-gateway-channel')} "
          f"attempted={headers.get('x-gateway-attempted')}")
    if broken:
        print(f"  ⚠ 流被中断：{broken}")
        print("     如果这里出现 NotImplementedError，说明 completions.py 里的 STREAM-1..4 还没填。")

    ok = (
        len(chunks) == N_CHUNKS
        and done
        and headers.get("x-gateway-channel") == "openai-primary"
        and "deepseek-primary" in (headers.get("x-gateway-attempted") or "")
        and headers.get("x-gateway-ttft-ms") is not None
    )
    print("  → " + ("✅ 通过" if ok else "❌ 不符合预期"))
    return ok


def case_all_dead() -> bool:
    print("\n" + "-" * 72)
    print("  场景 B · 两个通道都坏 → 一个 token 都没发出，应返回 502 + 结构化错误")
    print("-" * 72)
    body = {"model": "auto", "messages": [{"role": "user", "content": f"流式失败测试-{time.time():.0f}"}], "stream": True}
    with httpx.Client(timeout=60) as client:
        r = client.post(f"{GATEWAY}/v1/chat/completions", json=body)
    print(f"  状态码：{r.status_code}")
    try:
        data = r.json()
        print(f"  响应体：{json.dumps(data, ensure_ascii=False)[:220]}")
        ok = r.status_code in (502, 503, 504) and "code" in (data.get("error") or {})
    except Exception:
        print(f"  响应体（非 JSON）：{r.text[:200]}")
        ok = False
    print("  → " + ("✅ 通过" if ok else "❌ 不符合预期"))
    return ok


def main():
    print(LINE)
    print("  实验 6 · SSE 流式 + 首 token 超时切换")
    print(LINE)

    shutil.copy2(CONFIG, BACKUP)
    mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockSSE)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    print(f"  [mock] 流式上游：{MOCK_URL}（{N_CHUNKS} 个 chunk + [DONE]）")

    results = {}
    try:
        # 场景 A
        patch(dead=True, mock=True)
        with Gateway():
            results["流式 + 故障切换"] = case_stream()

        # 场景 B
        patch(dead=True, mock=False)
        with Gateway():
            results["全通道失败 → 502"] = case_all_dead()
    finally:
        mock.shutdown()
        shutil.copy2(BACKUP, CONFIG)
        print("\n[清理] 网关已关、mock 已关、providers.yaml 已还原")

    print("\n" + LINE)
    for name, ok in results.items():
        print(f"  {name:<24} {'✅ PASS' if ok else '❌ FAIL'}")
    print(LINE + "\n")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

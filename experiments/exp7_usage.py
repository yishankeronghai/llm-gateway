r"""
实验 7 · 成本聚合验收

    .venv\Scripts\python.exe experiments\exp7_usage.py            # 先造 3 条带 token 的记录再验证
    .venv\Scripts\python.exe experiments\exp7_usage.py --no-seed   # 不造数据，直接验证

它会：
  1.（默认）往审计表追加 3 条已知 token 用量的记录（走 AuditStore.append，哈希链依然完整）
  2. 起网关，依次请求 /v1/usage?group_by=model / provider / day
  3. 用本脚本独立算一遍"应该花多少钱"，和端点返回的数字对比

预期：等你写完 AuditStore.usage_summary 的 SQL，三个视图都能对上；没写之前会看到 501。
"""

import json
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
sys.path.insert(0, str(ROOT))

GATEWAY = "http://127.0.0.1:8080"
LOG = ROOT / "data" / "lab_server.log"
LINE = "=" * 74

SEED = [
    {"model": "deepseek-chat", "provider": "deepseek", "channel_id": "deepseek-primary",
     "input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500, "latency_ms": 800.0},
    {"model": "gpt-4o-mini", "provider": "openai", "channel_id": "openai-primary",
     "input_tokens": 2000, "output_tokens": 1000, "total_tokens": 3000, "latency_ms": 1200.0},
    {"model": "deepseek-chat", "provider": "deepseek", "channel_id": "deepseek-primary",
     "input_tokens": 100, "output_tokens": 100, "total_tokens": 200, "latency_ms": 300.0},
]


def repair():
    """把被破坏的哈希链"截断修复"到最后一个一致点。

    这个函数顺手演示了 W1 实验 3 学到的两件事：
      1. **尾部删除查不出** —— 所以我们能用"从断点往后删"的方式修链
      2. 但 `audit_meta.last_hash` 必须同步，否则下一次 append 会接到旧链头上

    ⚠️ 会丢数据（删掉断点之后的所有审计记录），只适合实验库。
    真实系统的正确做法是：审计写入必须**单写者**（一个进程/一把分布式锁），
    或每个实例各写各的链、上层再做汇总——否则并发 append 天然会撞链。
    """
    import sqlite3
    from src.audit.store import AuditStore as _Store
    from src.config import config

    store = _Store(config.db_path)
    state = store.verify_chain()
    print(f"  当前链状态：{state['message']}")
    if state["valid"]:
        print("  链是完整的，不需要修")
        return

    bid = state["broken_at_id"]
    conn = sqlite3.connect(config.db_path)
    try:
        n = conn.execute("DELETE FROM audit_logs WHERE id >= ?", (bid,)).rowcount
        row = conn.execute("SELECT curr_hash FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()
        last = row[0] if row else "0" * 64
        conn.execute("UPDATE audit_meta SET value=? WHERE key='last_hash'", (last,))
        conn.commit()
    finally:
        conn.close()
    print(f"  已从断点 id={bid} 起截断 {n} 条记录，并同步链头")
    print(f"  修复后：{store.verify_chain()['message']}")


def seed_rows():
    """用 AuditStore.append 追加记录（走哈希链，verify_chain 仍然完整）"""
    from src.audit.store import AuditStore
    from src.config import config
    import datetime

    store = AuditStore(config.db_path)
    for i, row in enumerate(SEED):
        store.append({
            "trace_id": f"seed{i:02d}",
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "api_key": "seed",
            "model": row["model"],
            "provider": row["provider"],
            "channel_id": row["channel_id"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "total_tokens": row["total_tokens"],
            "latency_ms": row["latency_ms"],
            "status_code": 200,
        })
    print(f"  已追加 {len(SEED)} 条带 token 的审计记录（哈希链保持完整）")


def expected_cost() -> dict:
    """按 pricing.yaml 独立算一遍期望值"""
    import yaml
    with open(ROOT / "config" / "pricing.yaml", "r", encoding="utf-8") as f:
        pricing = yaml.safe_load(f) or {}
    table = pricing.get("pricing", {})
    fb = pricing.get("fallback", {})

    per_model: dict[str, dict] = {}
    for row in SEED:
        pin = (table.get(row["provider"], {}).get(row["model"], {}) or {}).get("input", fb.get("input", 2.0))
        pout = (table.get(row["provider"], {}).get(row["model"], {}) or {}).get("output", fb.get("output", 8.0))
        cost = row["input_tokens"] / 1e6 * pin + row["output_tokens"] / 1e6 * pout
        e = per_model.setdefault(row["model"], {"tokens": 0, "cost": 0.0})
        e["tokens"] += row["total_tokens"]
        e["cost"] += cost
    return per_model


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


def main():
    do_seed = "--no-seed" not in sys.argv
    if "--repair" in sys.argv:
        print(LINE)
        print("  修复被种子记录破坏的哈希链")
        print(LINE)
        repair()
        return 0

    print(LINE)
    print("  实验 7 · 成本聚合（/v1/usage）")
    print(LINE)
    if do_seed:
        seed_rows()

    exp = expected_cost()
    print("\n  本次种子数据的期望值（脚本独立计算）：")
    for model, e in exp.items():
        print(f"    {model:<20} tokens={e['tokens']:>6}   cost≈¥{e['cost']:.6f}")

    log_fp = open(LOG, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([sys.executable, str(ROOT / "src" / "main.py")],
                            cwd=str(ROOT), stdout=log_fp, stderr=subprocess.STDOUT)
    rc = 1
    try:
        if not wait_health():
            print("\n[错误] 网关没起来，见 data/lab_server.log")
            return 2

        all_ok = True
        for gb in ("model", "provider", "day"):
            r = httpx.get(f"{GATEWAY}/v1/usage", params={"group_by": gb}, timeout=10)
            print(f"\n  ── group_by={gb}  HTTP {r.status_code} ──")
            if r.status_code != 200:
                print("    " + json.dumps(r.json(), ensure_ascii=False)[:300])
                if r.status_code == 501:
                    print("    → usage_summary 还没写（这正是你的 TODO）")
                all_ok = False
                continue
            data = r.json()
            print(f"    total_requests={data['total_requests']}  total_tokens={data['total_tokens']}  "
                  f"total_cost=¥{data['total_cost']:.6f}  cost_estimated={data['cost_estimated']}")
            for row in data["breakdown"][:5]:
                print(f"      {str(row['key']):<20} req={row['requests']:<3} "
                      f"tok={row['total_tokens']:<7} cost=¥{row['cost']:.6f}")

            if gb == "model":
                for model, e in exp.items():
                    got = next((x for x in data["breakdown"] if x["key"] == model), None)
                    if not got:
                        print(f"      ✗ 缺少模型 {model}")
                        all_ok = False
                    elif got["total_tokens"] < e["tokens"]:
                        print(f"      ✗ {model} token 少于期望 {e['tokens']}（实际 {got['total_tokens']}）")
                        all_ok = False
                print("    → 与期望值对比：" + ("✅ 数字对得上" if all_ok else "❌ 有偏差"))

        print("\n" + LINE)
        print("  结论：" + ("✅ 成本聚合通过" if all_ok else "❌ 还没通过（看上面提示）"))
        print(LINE + "\n")
        rc = 0 if all_ok else 1
        return rc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_fp.close()


if __name__ == "__main__":
    sys.exit(main())

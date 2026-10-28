r"""
实验 3 助手：篡改审计库，当场验证哈希链。
不需要启动 HTTP 服务，也不需要在命令行里内联写 SQL（PowerShell 会把 SQL 里的括号当命令解析）。

用法（在任意目录都行，路径按脚本位置算）：

    .venv\Scripts\python.exe experiments\lab3_tamper.py verify
    .venv\Scripts\python.exe experiments\lab3_tamper.py tamper-provider
    .venv\Scripts\python.exe experiments\lab3_tamper.py tamper-error
    .venv\Scripts\python.exe experiments\lab3_tamper.py delete-last
    .venv\Scripts\python.exe experiments\lab3_tamper.py delete-middle
    .venv\Scripts\python.exe experiments\lab3_tamper.py verify-real    # 检查真实库（应该始终完整）

重要：每个实验都在**副本** data/gateway.lab.db 上做，你的真实审计库不会被改动。
每次运行会自动重建副本，所以五个实验可以随便重复、不需要还原。
"""

import shutil
import sqlite3
import sys
from pathlib import Path

# Windows 控制台默认 GBK，避免中文/符号编码报错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.audit.store import AuditStore  # noqa: E402

DB = ROOT / "data" / "gateway.db"          # 真实库：只读，不改
LAB = ROOT / "data" / "gateway.lab.db"     # 实验副本：每次重建


def chain_state(db_path: Path) -> str:
    if not db_path.exists():
        return "（文件不存在）"
    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    finally:
        conn.close()
    res = AuditStore(str(db_path)).verify_chain()
    tag = "[完整 OK]" if res["valid"] else f"[断链 BROKEN@id={res.get('broken_at_id')}]"
    return f"记录数={n:>3}  链状态={tag}  [{res['message']}]"


def fresh_lab():
    if not DB.exists():
        print(f"[错误] 真实库不存在：{DB}")
        print("      先启动网关、发几个请求（不带 key 也会写审计记录），再回来做实验。")
        sys.exit(2)
    shutil.copy2(DB, LAB)


def first_id() -> int:
    conn = sqlite3.connect(LAB)
    try:
        row = conn.execute("SELECT MIN(id) FROM audit_logs").fetchone()
        return row[0] if row and row[0] is not None else -1
    finally:
        conn.close()


def run_sql(sql: str, params: tuple = ()) -> int:
    conn = sqlite3.connect(LAB)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def experiment(title: str, expectation: str, sql: str, params: tuple = ()):
    fresh_lab()
    print(f"\n{'=' * 68}")
    print(title)
    print(f"{'=' * 68}")
    print("  篡改前：", chain_state(LAB))
    rid = first_id()
    if rid < 0:
        print("  [错误] 副本里没有记录")
        return
    bind: tuple = params if params else ((rid,) if "?" in sql else ())
    n = run_sql(sql, bind)
    print(f"  执行：{sql.strip()}  （影响 {n} 行）")
    print("  篡改后：", chain_state(LAB))
    print(f"  预期：{expectation}")


def main():
    op = sys.argv[1] if len(sys.argv) > 1 else "verify"

    if op == "verify":
        fresh_lab()
        print("\n实验副本（每次重建）：", chain_state(LAB))
        print("真实库（未被改动）：", chain_state(DB), "\n")
        return 0

    if op == "verify-real":
        print("\n真实库：", chain_state(DB), "\n")
        return 0

    if op == "tamper-provider":
        experiment(
            "实验 A · 改第 1 条的 provider（provider 在 hash_data 里）",
            "❌ 应该断链 —— 数据变了，重算的 curr_hash 对不上库里存的 curr_hash",
            "UPDATE audit_logs SET provider='hacked' WHERE id=?",
        )

    elif op == "tamper-error":
        experiment(
            "实验 B · 改第 1 条的 error 字段（error 不在 hash_data 里）",
            "✅ 依然完整 —— 这是盲区①：没被纳入哈希的字段，改了查不出",
            "UPDATE audit_logs SET error='被人改过的错误信息' WHERE id=?",
        )

    elif op == "delete-last":
        experiment(
            "实验 C · 删掉最后一条",
            "✅ 依然完整 —— 这是盲区②：没有记录引用它的 hash，链照样自洽",
            "DELETE FROM audit_logs WHERE id=(SELECT MAX(id) FROM audit_logs)",
        )

    elif op == "delete-middle":
        experiment(
            "实验 D · 删掉中间一条",
            "❌ 应该断链 —— 后一条的 prev_hash 指向了被删掉的那条",
            "DELETE FROM audit_logs WHERE id=(SELECT id FROM audit_logs ORDER BY id LIMIT 1 OFFSET 2)",
        )

    else:
        print(f"[错误] 不认识的命令：{op}")
        print("可用：verify | verify-real | tamper-provider | tamper-error | delete-last | delete-middle")
        return 2

    print("\n-> 真实库不受影响。做完跑 verify 看两个库的对比。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

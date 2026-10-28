r"""
哈希链·可视化讲解（不连数据库，纯内存，5 条迷你记录）

    .venv\Scripts\python.exe experiments\lab3_chain_walk.py

跑一遍你会看到 5 个场景：
    场景 1  正常链          → 校验通过
    场景 2  改第 2 条数据   → 断在第 2 条
    场景 3  删中间一条      → 断在被删那条的"下一条"
    场景 4  删最后一条      → 校验通过（盲区！）
    场景 5  改数据 + 把后面全重算 → 校验通过（所以必须"外部锚定"）
"""

import hashlib
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

GENESIS = "0" * 64


def fingerprint(data: dict, prev_hash: str) -> str:
    """本行指纹 = SHA256( 上一行指纹 + 本行数据 )"""
    payload = prev_hash + json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_chain(n: int = 5) -> list[dict]:
    rows, prev = [], GENESIS
    for i in range(1, n + 1):
        data = {"trace_id": f"t{i}", "provider": "deepseek", "tokens": i * 10}
        curr = fingerprint(data, prev)
        rows.append({"id": i, "data": data, "prev": prev, "curr": curr})
        prev = curr
    return rows


def verify(rows: list[dict], verbose: bool = True) -> int | None:
    """返回断链位置（None 表示通过）。逻辑与 src/audit/store.py 的 verify_chain 一致。"""
    prev = GENESIS
    for row in rows:
        expect = fingerprint(row["data"], prev)
        if verbose:
            print(
                f"    id={row['id']:<2} 存的prev={row['prev'][:8]} 算出的prev={prev[:8]} "
                f"{'✔' if row['prev'] == prev else 'X  <- 断链！prev 对不上'}"
            )
        if row["prev"] != prev:
            return row["id"]
        if row["curr"] != expect:
            if verbose:
                print(f"        ↑ 而且数据算出的指纹 {expect[:8]} ≠ 存的 {row['curr'][:8]}  ← 断链！内容被改")
            return row["id"]
        prev = row["curr"]
    return None


def show_table(rows: list[dict], title: str):
    print(f"\n{title}")
    print("    " + "-" * 66)
    print("    id | provider | tokens | prev(前8位) | curr(前8位)")
    print("    " + "-" * 66)
    for r in rows:
        print(
            f"    {r['id']:<2} | {r['data']['provider']:<8} | {r['data']['tokens']:>6} | "
            f"{r['prev'][:8]:<11} | {r['curr'][:8]}"
        )
    print("    " + "-" * 66)


def result(broken):
    if broken is None:
        print("    → 校验结果：✅ 通过（链完整）")
    else:
        print(f"    → 校验结果：❌ 断链，位置 id={broken}")


def main():
    print("=" * 70)
    print("  哈希链原理：每行的指纹，把上一行的指纹也算进去")
    print("=" * 70)
    print("\n公式：curr_hash = SHA256( prev_hash + 本行数据 )")
    print("第一行的 prev_hash 是一个约定值：64 个 0")

    # 场景 1
    rows = build_chain(5)
    show_table(rows, "场景 1 · 正常链")
    print("\n  逐行校验（校验器视角）：")
    result(verify(rows))

    # 场景 2
    rows = build_chain(5)
    rows[1]["data"]["provider"] = "hacked"
    show_table(rows, "场景 2 · 把第 2 条的 provider 改成 hacked（数据变了，指纹没变）")
    print("\n  逐行校验：")
    result(verify(rows))
    print("\n  ⚠ 关键：如果攻击者顺手也把第 2 条的指纹重算写进去，")
    print("    那第 3 条的 prev 还是旧指纹，断链会挪到第 3 条。要改就得一路重算到底。")

    # 场景 3
    rows = build_chain(5)
    del rows[2]  # 删掉 id=3
    show_table(rows, "场景 3 · 删掉中间那条（id=3）")
    print("\n  逐行校验：")
    result(verify(rows))
    print("\n  ⚠ 关键：id=4 的 prev 里写着 id=3 的指纹，但 id=3 那行已经没了。")
    print("    链接存在的是那个『指纹值』，不是那一行——所以删中间会暴露。")

    # 场景 4
    rows = build_chain(5)
    rows.pop()  # 删最后一条
    show_table(rows, "场景 4 · 删掉最后一条（id=5）")
    print("\n  逐行校验：")
    result(verify(rows))
    print("\n  ⚠ 这就是盲区：id=5 的指纹没有被任何后续行引用过，")
    print("    删了它，前面 1~4 依然自洽。校验器只能证明『链内自洽』，")
    print("    它不知道『链原本有多长』。攻击者从尾部往前删，一路都骗得过。")

    # 场景 5
    rows = build_chain(5)
    rows[1]["data"]["provider"] = "hacked"
    prev = GENESIS
    for r in rows:                      # 攻击者把整条链的指纹全部重算一遍
        r["prev"] = prev
        r["curr"] = fingerprint(r["data"], prev)
        prev = r["curr"]
    show_table(rows, "场景 5 · 改第 2 条，并把后面所有指纹全部重算（聪明的攻击者）")
    print("\n  逐行校验：")
    result(verify(rows))
    print("\n  ⚠ 结论：单靠自己算出来的链，防不住『有写权限的攻击者全量重算』。")
    print("    所以生产上必须做『外部锚定』：定期把链头指纹(A24)记到链外——")
    print("    写进 Git 提交、发到另一台机器、写进当天日报。事后一比对就露馅。")

    print("\n" + "=" * 70)
    print("  一句话记忆：")
    print("   哈希链能证明『历史没被改过、顺序没被删过（中间）』，")
    print("   但证明不了『历史有多长』——长度要靠外部锚定来背书。")
    print("=" * 70 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

r"""
W2 验收器 · 断路器重写自动判分
（这是我的判分脚本，不是你实现的一部分。不要修改这个文件。）

用法：
    .venv\Scripts\python.exe experiments\w2_breaker_check.py

它会 import 你写的 experiments\w2_breaker.py，跑 9 组行为检查。
你只需要提供：CircuitState / CircuitBreaker / CircuitBreakerManager / breaker_manager 四个名字。
"""

import asyncio
import importlib.util
import sys
import traceback
from pathlib import Path

# Windows 控制台默认 GBK，避免中文/符号编码报错把正常结果变成 exit 1
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CANDIDATE = Path(__file__).parent / "w2_breaker.py"

results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok))
    print(f"  {'[PASS]' if ok else '[FAIL]'} {name}" + (f"  ← {detail}" if detail and not ok else ""))


def load_candidate():
    if not CANDIDATE.exists():
        print(f"\n[错误] 没找到 {CANDIDATE}")
        print("       先去 docs/W2_REWRITE.md，按规格在 experiments/w2_breaker.py 里写你的实现。\n")
        sys.exit(2)
    spec = importlib.util.spec_from_file_location("w2_breaker", CANDIDATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["w2_breaker"] = mod
    spec.loader.exec_module(mod)
    return mod


async def main():
    m = load_candidate()

    for attr in ("CircuitState", "CircuitBreaker", "CircuitBreakerManager", "breaker_manager"):
        if not hasattr(m, attr):
            print(f"\n[错误] 你的模块里缺少 `{attr}`（接口规格见 docs/W2_REWRITE.md）\n")
            sys.exit(2)

    CircuitBreaker = m.CircuitBreaker
    CircuitBreakerManager = m.CircuitBreakerManager
    st = lambda cb: str(getattr(cb.state, "value", cb.state))

    print("\n" + "=" * 62)
    print("  W2 断路器重写 · 行为验收（9 组 / 共 16 条断言）")
    print("=" * 62 + "\n")

    print("[1] 初始状态应为 closed，且允许通过")
    try:
        cb = CircuitBreaker(name="t1")
        ok = st(cb) == "closed" and await cb.can_execute() is True
        check("初始 closed + can_execute True", ok, f"state={st(cb)}")
    except Exception as e:
        check("初始 closed + can_execute True", False, f"{type(e).__name__}: {e}")

    print("[2] 连续失败 4 次仍 closed，第 5 次转 open（阈值=5）")
    try:
        cb = CircuitBreaker(name="t2", failure_threshold=5, recovery_timeout=60)
        for _ in range(4):
            await cb.record_failure()
        state_at_4 = st(cb)
        await cb.record_failure()
        check("第 4 次仍 closed", state_at_4 == "closed", f"state={state_at_4}")
        check("第 5 次转 open", st(cb) == "open", f"state={st(cb)}")
    except Exception as e:
        check("阈值触发熔断", False, f"{type(e).__name__}: {e}")

    print("[3] open 期间必须拒绝（can_execute False）")
    try:
        allowed = await cb.can_execute()
        check("open 时 can_execute False", allowed is False, f"返回 {allowed}")
    except Exception as e:
        check("open 时 can_execute False", False, f"{type(e).__name__}: {e}")

    print("[4] 冷却到期自动 half_open，且只放行 1 个探测请求")
    try:
        cb = CircuitBreaker(name="t4", failure_threshold=2, recovery_timeout=0.2, half_open_max_calls=1)
        await cb.record_failure()
        await cb.record_failure()
        assert st(cb) == "open", f"预期 open，实际 {st(cb)}"
        await asyncio.sleep(0.25)
        s_after = st(cb)  # 读取 state 时应该已自动转 half_open
        first = await cb.can_execute()
        second = await cb.can_execute()
        check("冷却后自动转 half_open", s_after == "half_open", f"state={s_after}")
        check("第 1 个探测放行", first is True)
        check("第 2 个探测拒绝", second is False, f"返回 {second}")
    except Exception as e:
        check("half_open 探测配额", False, f"{type(e).__name__}: {e}")

    print("[5] half_open 下探测成功 → 回到 closed，失败计数清零")
    try:
        cb = CircuitBreaker(name="t5", failure_threshold=2, recovery_timeout=0.2, half_open_max_calls=1)
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.25)
        await cb.can_execute()          # 占掉探测名额
        await cb.record_success()
        d = cb.to_dict() if hasattr(cb, "to_dict") else {}
        check("探测成功后 closed", st(cb) == "closed", f"state={st(cb)}")
        check("失败计数归零", d.get("failure_count") == 0, f"failure_count={d.get('failure_count')}")
    except Exception as e:
        check("half_open 探测成功恢复", False, f"{type(e).__name__}: {e}")

    print("[6] half_open 下探测失败 → 回到 open，且冷却重新计时")
    try:
        cb = CircuitBreaker(name="t6", failure_threshold=2, recovery_timeout=0.3, half_open_max_calls=1)
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.35)
        await cb.can_execute()
        await cb.record_failure()       # 探测失败
        s_now = st(cb)
        allowed_now = await cb.can_execute()
        check("探测失败回到 open", s_now == "open", f"state={s_now}")
        check("冷却重新计时（立即被拒）", allowed_now is False, f"返回 {allowed_now}")
    except Exception as e:
        check("half_open 探测失败重熔断", False, f"{type(e).__name__}: {e}")

    print("[7] closed 状态下一次成功应清零失败计数（4败+1成+1败 不该熔断）")
    try:
        cb = CircuitBreaker(name="t7", failure_threshold=5, recovery_timeout=60)
        for _ in range(4):
            await cb.record_failure()
        await cb.record_success()
        await cb.record_failure()
        check("成功清零失败计数", st(cb) == "closed", f"state={st(cb)}")
    except Exception as e:
        check("成功清零失败计数", False, f"{type(e).__name__}: {e}")

    print("[8] Manager：同 channel 返回同一实例，不同 channel 隔离")
    try:
        mgr = CircuitBreakerManager()
        a1, a2 = mgr.get("ch-a"), mgr.get("ch-a")
        b = mgr.get("ch-b")
        await a1.record_failure()
        check("同 channel 同实例", a1 is a2)
        check("不同 channel 互相隔离", st(b) == "closed" and a1 is not b, f"ch-b state={st(b)}")
        status = mgr.all_status()
        check("all_status 返回状态列表", isinstance(status, list) and len(status) == 2, f"len={len(status) if isinstance(status, list) else 'N/A'}")
    except Exception as e:
        check("Manager 行为", False, f"{type(e).__name__}: {e}")

    print("[9] 并发安全：closed 下 20 个并发 can_execute 全部放行")
    try:
        cb = CircuitBreaker(name="t9")
        got = await asyncio.gather(*[cb.can_execute() for _ in range(20)])
        check("并发全放行", all(g is True for g in got), f"拒绝数={sum(1 for g in got if g is not True)}")
    except Exception as e:
        check("并发安全", False, f"{type(e).__name__}: {e}")

    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("\n" + "=" * 62)
    print(f"  结果: {passed}/{total} 通过")
    if passed == total:
        print("  [OK] W2 断路器全部通过。接着跑：.venv\\Scripts\\python.exe test_failover.py（应 3/3）")
    else:
        print("  未通过项对着 docs/W2_REWRITE.md 的『行为规格』逐条看")
    print("=" * 62 + "\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)

"""
快速测试脚本 - 一键验证网关所有功能
================================
运行: python quick_test.py
前提: 网关已启动 (python src/main.py)
"""

import httpx
import json

GATEWAY = "http://localhost:8080"
client = httpx.Client(base_url=GATEWAY, timeout=120)


def test(name, func):
    """运行一个测试"""
    print(f"\n{'='*50}")
    print(f"测试: {name}")
    print(f"{'='*50}")
    try:
        result = func()
        status = "PASS" if result else "FAIL"
        print(f"\n>>> 结果: {status}")
        return result
    except Exception as e:
        print(f"\n>>> 结果: ERROR - {e}")
        return False


def test_health():
    """测试 1: 健康检查"""
    r = client.get("/health")
    print(f"GET /health -> {r.status_code}")
    print(f"Response: {r.json()}")
    return r.status_code == 200


def test_status():
    """测试 2: 网关状态"""
    r = client.get("/v1/status")
    data = r.json()
    print(f"GET /v1/status -> {r.status_code}")
    print(f"Providers: {data.get('providers', 'N/A')}")
    print(f"Models: {data.get('models', [])}")
    print(f"Load balancing: {data.get('load_balancing', 'N/A')}")
    return r.status_code == 200


def test_chat():
    """测试 3: 基础对话调用"""
    print("发送: model=deepseek-v4-flash, message='1+1=?'")
    r = client.post("/v1/chat/completions", json={
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "1+1=?"}],
    })
    print(f"POST /v1/chat/completions -> {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        model = data.get("model", "?")
        print(f"Model used: {model}")
        print(f"Reply: {content}")
        print(f"Tokens: {usage}")
        return True
    else:
        print(f"Error: {r.text}")
        return False


def test_alias():
    """测试 4: 模型别名"""
    print("发送: model=flash (别名)")
    r = client.post("/v1/chat/completions", json={
        "model": "flash",
        "messages": [{"role": "user", "content": "说一个数字"}],
    })
    print(f"POST (alias=flash) -> {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"Resolved model: {data.get('model', '?')}")
        print(f"Reply: {data['choices'][0]['message']['content']}")
        return True
    else:
        print(f"Error: {r.text}")
        return False


def test_prompt_template():
    """测试 5: 提示词模板"""
    print("发送: prompt_id=code-reviewer, vars={language:Python, focus:security}")
    r = client.post("/v1/chat/completions", json={
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "def add(a, b):\n    return a+b"}],
        "prompt_id": "code-reviewer",
        "prompt_vars": {"language": "Python", "focus": "security"},
    })
    print(f"POST (with prompt) -> {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        print(f"Reply:\n{content[:300]}...")
        return True
    else:
        print(f"Error: {r.text}")
        return False


def test_dry_run():
    """测试 6: 提示词 Dry-run"""
    print("发送: POST /v1/prompts/code-reviewer/dry-run")
    r = client.post("/v1/prompts/code-reviewer/dry-run", json={
        "variables": {"language": "Go", "focus": "performance"}
    })
    print(f"POST /v1/prompts/.../dry-run -> {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        rendered = data.get("rendered_template", "")
        print(f"Version: {data.get('version')}")
        print(f"Rendered (first 200 chars):\n{rendered[:200]}...")
        return True
    else:
        print(f"Error: {r.text}")
        return False


def test_audit():
    """测试 7: 审计日志"""
    r = client.get("/v1/audit/logs?limit=5")
    print(f"GET /v1/audit/logs?limit=5 -> {r.status_code}")
    logs = r.json()
    if isinstance(logs, list):
        print(f"Got {len(logs)} log entries")
        for i, log in enumerate(logs[:3]):
            print(f"  [{i+1}] {log.get('timestamp','')[:19]} | "
                  f"{'OK' if log.get('success') else 'FAIL'} | "
                  f"{log.get('provider','')}/{log.get('channel','')} | "
                  f"{log.get('latency_ms',0)}ms")

    r2 = client.get("/v1/audit/verify")
    verify = r2.json()
    print(f"\nHash chain verify: {verify}")
    return r.status_code == 200


def test_audit_stats():
    """测试 8: 审计统计"""
    r = client.get("/v1/audit/stats")
    print(f"GET /v1/audit/stats -> {r.status_code}")
    data = r.json()
    print(f"Total: {data.get('total', 0)}")
    print(f"Success: {data.get('success', 0)} | Failure: {data.get('failure', 0)}")
    print(f"Avg latency: {data.get('avg_latency_ms', 0):.0f} ms")
    print(f"Total tokens: {data.get('total_tokens', 0)}")
    return r.status_code == 200


if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  LLM Gateway 快速测试")
    print("=" * 50)

    results = []
    results.append(test("健康检查", test_health))
    results.append(test("网关状态", test_status))
    results.append(test("基础对话调用", test_chat))
    results.append(test("模型别名 (flash)", test_alias))
    results.append(test("提示词模板 (code-reviewer)", test_prompt_template))
    results.append(test("提示词 Dry-run", test_dry_run))
    results.append(test("审计日志", test_audit))
    results.append(test("审计统计", test_audit_stats))

    print("\n" + "=" * 50)
    passed = sum(results)
    total = len(results)
    print(f"  结果: {passed}/{total} 通过")
    print("=" * 50 + "\n")

"""
LLM Gateway 测试脚本
无需真实 API Key 即可验证网关核心功能

用法: python tests/test_gateway.py
"""

import httpx
import time
import json
import sys

BASE_URL = "http://localhost:8080"
client = httpx.Client(base_url=BASE_URL, timeout=10)


def test_health():
    """测试健康检查"""
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    print("  [PASS] 健康检查正常")
    return True


def test_models():
    """测试模型列表"""
    r = client.get("/v1/models")
    assert r.status_code == 200
    data = r.json()
    assert len(data["data"]) > 0
    print(f"  [PASS] 可用模型: {len(data['data'])} 个")
    return True


def test_status():
    """测试网关状态"""
    r = client.get("/v1/status")
    assert r.status_code == 200
    data = r.json()
    assert len(data["providers"]) > 0
    print(f"  [PASS] 提供商: {len(data['providers'])} 个")
    print(f"        负载均衡策略: {data['load_balancing']['strategy']}")
    return True


def test_prompts_list():
    """测试提示词列表"""
    r = client.get("/v1/prompts")
    assert r.status_code == 200
    prompts = r.json()
    assert len(prompts) > 0
    print(f"  [PASS] 提示词模板: {len(prompts)} 个")
    for p in prompts:
        versions = [v["version"] for v in p["versions"]]
        print(f"        - {p['id']}: 版本 {versions}")
    return True


def test_prompt_dry_run():
    """测试提示词 Dry-run"""
    r = client.post(
        "/v1/prompts/code-reviewer/dry-run",
        json={"variables": {"language": "Go", "focus": "performance"}},
    )
    assert r.status_code == 200
    data = r.json()
    assert "Python" not in data["rendered_template"]
    assert "Go" in data["rendered_template"]
    print(f"  [PASS] 提示词渲染: language=Go, version={data['version']}")
    return True


def test_chat_completion():
    """测试聊天补全 (无 API Key 会失败，但验证路由链路)"""
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Hello"}],
        },
        timeout=45,
    )
    # 没有 API Key 会返回 200 (错误响应) 或 502
    if r.status_code == 200:
        data = r.json()
        gw = data.get("x_gateway", {})
        print(f"  [PASS] 路由成功: provider={gw.get('provider')}, channel={gw.get('channel')}")
    elif r.status_code == 502:
        print(f"  [EXPECT] 路由失败(无API Key): {r.json().get('detail', '')[:50]}")
    return True


def test_audit_logs():
    """测试审计日志"""
    r = client.get("/v1/audit/logs?limit=10")
    assert r.status_code == 200
    data = r.json()
    print(f"  [PASS] 审计日志: {data['count']} 条")
    if data["logs"]:
        log = data["logs"][0]
        print(f"        最近一条: trace={log['trace_id']}, hash={log['curr_hash'][:16]}...")
    return True


def test_audit_verify():
    """测试哈希链验证"""
    r = client.get("/v1/audit/verify")
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] == True
    print(f"  [PASS] 哈希链验证: {data['message']} ({data['total']} 条记录)")
    return True


def test_audit_stats():
    """测试审计统计"""
    r = client.get("/v1/audit/stats")
    assert r.status_code == 200
    data = r.json()
    print(f"  [PASS] 审计统计: 请求={data['total_requests']}, 成功率={data['success_rate']}%")
    return True


def test_chat_with_prompt():
    """测试带提示词模板的聊天补全"""
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "请审查这段代码"}],
            "prompt_id": "code-reviewer",
            "prompt_vars": {"language": "Python", "focus": "security"},
        },
        timeout=45,
    )
    if r.status_code == 200:
        data = r.json()
        gw = data.get("x_gateway", {})
        prompt_info = gw.get("prompt")
        if prompt_info:
            print(f"  [PASS] 提示词注入: id={prompt_info.get('id')}, version={prompt_info.get('version')}")
        else:
            print(f"  [PASS] 请求完成 (无提示词信息)")
    elif r.status_code == 502:
        print(f"  [EXPECT] 路由失败(无API Key)")
    return True


def main():
    print()
    print("=" * 50)
    print("  LLM Gateway 功能测试")
    print("=" * 50)
    print()

    tests = [
        ("健康检查", test_health),
        ("模型列表", test_models),
        ("网关状态", test_status),
        ("提示词列表", test_prompts_list),
        ("提示词 Dry-run", test_prompt_dry_run),
        ("聊天补全(无Key)", test_chat_completion),
        ("带提示词的补全", test_chat_with_prompt),
        ("审计日志", test_audit_logs),
        ("哈希链验证", test_audit_verify),
        ("审计统计", test_audit_stats),
    ]

    passed = 0
    for name, test in tests:
        print(f"[{name}]")
        try:
            if test():
                passed += 1
        except Exception as e:
            print(f"  [FAIL] {e}")
        print()

    print("=" * 50)
    print(f"  结果: {passed}/{len(tests)} 通过")
    print("=" * 50)
    print()

    # 提示访问仪表盘
    print(f"  仪表盘: {BASE_URL}/")
    print()


if __name__ == "__main__":
    main()

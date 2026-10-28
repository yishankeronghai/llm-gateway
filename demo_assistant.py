"""
智能语言助手 Demo - 通过 LLM Gateway 调用大模型
============================================
这个脚本演示了如何通过本地网关系统调用 AI：
  1. 多轮对话（带上下文记忆）
  2. 提示词模板（代码审查、翻译、摘要）
  3. 审计日志查看

使用前提：网关已启动（python src/main.py）
"""

import sys
import os
import httpx

# ============================================================
# 网关地址 - 改这里就能切换网关实例
# ============================================================
GATEWAY_URL = "http://localhost:8080"
client = httpx.Client(base_url=GATEWAY_URL, timeout=120)


def print_banner():
    print()
    print("=" * 60)
    print("  智能语言助手  (通过 LLM Gateway 调用)")
    print("=" * 60)
    print(f"  网关地址: {GATEWAY_URL}")
    print("  输入 :help 查看命令  |  :quit 退出")
    print("=" * 60)
    print()


def check_gateway():
    """检查网关是否在线"""
    try:
        r = client.get("/health")
        if r.status_code == 200:
            print("[OK] 网关已连接")
            status = client.get("/v1/status").json()
            print(f"  提供商: {status.get('providers', 'N/A')}")
            print(f"  可用模型: {', '.join(status.get('models', []))}")
            print(f"  负载均衡: {status.get('load_balancing', 'N/A')}")
            print()
            return True
        else:
            print(f"[ERROR] 网关返回异常状态码: {r.status_code}")
            return False
    except Exception:
        print("[ERROR] 无法连接到网关！")
        print(f"  请先启动网关: cd llm-gateway && python src/main.py")
        print(f"  确保已设置环境变量: DEEPSEEK_API_KEY")
        return False


def chat(messages, model="deepseek-v4-flash", prompt_id=None, prompt_vars=None):
    """
    调用网关的 /v1/chat/completions 端点
    支持：
      - 普通对话（只传 messages）
      - 提示词模板（额外传 prompt_id + prompt_vars）
    """
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 2000,
    }

    if prompt_id:
        payload["prompt_id"] = prompt_id
        payload["prompt_vars"] = prompt_vars or {}

    try:
        r = client.post("/v1/chat/completions", json=payload)

        if r.status_code == 200:
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            model_used = data.get("model", model)
            return content, usage, model_used, None
        else:
            try:
                error_msg = r.json().get("error", {}).get("message", r.text)
            except Exception:
                error_msg = r.text
            return None, None, None, f"HTTP {r.status_code}: {error_msg}"

    except httpx.TimeoutException:
        return None, None, None, "请求超时（可能 API Key 未设置或无效）"
    except Exception as e:
        return None, None, None, str(e)


def mode_interactive():
    """模式 1：交互式多轮对话"""
    print("\n--- 交互式对话模式 ---")
    print("直接输入问题，AI 会记住上下文。输入 :back 撤回上一轮，:clear 清空对话。\n")

    history = []

    while True:
        try:
            user_input = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue

        if user_input == ":quit":
            break
        elif user_input == ":back":
            if len(history) >= 2:
                history = history[:-2]
                print("[已撤回上一轮对话]\n")
            else:
                print("[没有可撤回的对话]\n")
            continue
        elif user_input == ":clear":
            history = []
            print("[对话已清空]\n")
            continue
        elif user_input == ":help":
            print_help()
            continue
        elif user_input == ":audit":
            show_audit()
            continue

        history.append({"role": "user", "content": user_input})

        print("AI > ", end="", flush=True)
        content, usage, model_used, error = chat(history)

        if error:
            print(f"[错误] {error}\n")
            history.pop()
            continue

        print(content)
        print()

        history.append({"role": "assistant", "content": content})

        if usage:
            print(f"  [tokens: {usage.get('total_tokens', '?')} | model: {model_used}]")
            print()


def mode_code_review():
    """模式 2：代码审查（使用提示词模板）"""
    print("\n--- 代码审查模式 ---")
    print("粘贴你的代码，输入 END 结束。\n")

    code_lines = []
    print("代码 > ", end="", flush=True)
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            break
        if line.strip().upper() == "END":
            break
        code_lines.append(line)

    code = "\n".join(code_lines)
    if not code.strip():
        print("[代码为空]\n")
        return

    language = input("语言 (Python/Go/Java/...): ").strip() or "Python"
    focus = input("关注点 (general/security/performance/readability): ").strip() or "general"

    print(f"\n正在通过网关调用 deepseek-v4-flash，使用 code-reviewer 提示词模板...\n")
    print("审查结果:\n")

    messages = [{"role": "user", "content": f"请审查以下代码：\n\n```\n{code}\n```"}]

    content, usage, model_used, error = chat(
        messages,
        prompt_id="code-reviewer",
        prompt_vars={"language": language, "focus": focus},
    )

    if error:
        print(f"[错误] {error}\n")
        return

    print(content)
    print()
    if usage:
        print(f"  [tokens: {usage.get('total_tokens', '?')} | 模板: code-reviewer v2]")
    print()


def mode_translate():
    """模式 3：翻译（使用提示词模板）"""
    print("\n--- 翻译模式 ---")

    text = input("输入要翻译的文本: ").strip()
    if not text:
        return

    source = input("源语言 (默认: 中文): ").strip() or "中文"
    target = input("目标语言 (默认: 英文): ").strip() or "英文"

    print(f"\n正在翻译 {source} -> {target}...\n")

    messages = [{"role": "user", "content": text}]

    content, usage, model_used, error = chat(
        messages,
        prompt_id="translator",
        prompt_vars={"source_lang": source, "target_lang": target, "text": text},
    )

    if error:
        print(f"[错误] {error}\n")
        return

    print("翻译结果:")
    print(content)
    print()


def mode_summarize():
    """模式 4：文本摘要（使用提示词模板）"""
    print("\n--- 文本摘要模式 ---")
    print("粘贴要摘要的文本，输入 END 结束。\n")

    text_lines = []
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            break
        if line.strip().upper() == "END":
            break
        text_lines.append(line)

    text = "\n".join(text_lines)
    if not text.strip():
        print("[文本为空]\n")
        return

    max_words = input("摘要字数上限 (默认 100): ").strip() or "100"

    print(f"\n正在生成摘要...\n")

    messages = [{"role": "user", "content": text}]

    content, usage, model_used, error = chat(
        messages,
        prompt_id="summarizer",
        prompt_vars={"max_words": max_words, "content": text},
    )

    if error:
        print(f"[错误] {error}\n")
        return

    print("摘要:")
    print(content)
    print()


def show_audit():
    """显示审计日志"""
    print("\n--- 审计日志 ---\n")

    try:
        stats = client.get("/v1/audit/stats").json()
        print(f"总请求: {stats.get('total', 0)}")
        print(f"成功: {stats.get('success', 0)} | 失败: {stats.get('failure', 0)}")
        print(f"平均延迟: {stats.get('avg_latency_ms', 0):.0f} ms")
        print(f"总 Token: {stats.get('total_tokens', 0)}")
        print()

        logs = client.get("/v1/audit/logs?limit=10").json()
        if isinstance(logs, list) and logs:
            print(f"最近 {len(logs)} 条记录:\n")
            for log in logs:
                status_icon = "OK" if log.get("success") else "FAIL"
                ts = log.get("timestamp", "")[:19]
                provider = log.get("provider", "?")
                channel = log.get("channel", "?")
                model = log.get("model", "?")
                latency = log.get("latency_ms", 0)
                tokens = log.get("total_tokens", 0)
                trace = log.get("trace_id", "?")[:12]

                print(f"  [{ts}] {status_icon} | {provider}/{channel}")
                print(f"    model={model} | latency={latency}ms | tokens={tokens}")
                print(f"    trace={trace}")
                print()
        else:
            print("  (暂无日志)\n")

        verify = client.get("/v1/audit/verify").json()
        intact = verify.get("intact", False)
        print(f"哈希链验证: {'OK - 完整' if intact else 'WARNING - 链断裂！'}")
        print()

    except Exception as e:
        print(f"[错误] {e}\n")


def show_prompts():
    """显示可用提示词模板"""
    print("\n--- 提示词模板 ---\n")
    try:
        data = client.get("/v1/prompts").json()
        prompts = data if isinstance(data, list) else data.get("prompts", [])
        for p in prompts:
            print(f"  ID: {p.get('id')}")
            print(f"  名称: {p.get('name')}")
            print(f"  描述: {p.get('description')}")
            print(f"  版本: {p.get('active_version')} (共 {len(p.get('versions', []))} 个版本)")
            print()
    except Exception as e:
        print(f"[错误] {e}\n")


def print_help():
    print("""
命令:
  :help     - 显示帮助
  :quit     - 退出
  :audit    - 查看审计日志
  :clear    - 清空对话历史
  :back     - 撤回上一轮

模式:
  1 - 交互对话 (多轮，带上下文记忆)
  2 - 代码审查 (使用 code-reviewer 模板)
  3 - 翻译     (使用 translator 模板)
  4 - 文本摘要 (使用 summarizer 模板)
  5 - 查看审计日志
  6 - 查看提示词模板
""")


def main():
    print_banner()

    if not check_gateway():
        sys.exit(1)

    while True:
        print("选择模式: 1=对话  2=代码审查  3=翻译  4=摘要  5=审计  6=提示词  :help=帮助")
        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if choice == ":quit":
            break
        elif choice == ":help":
            print_help()
        elif choice == "1":
            mode_interactive()
        elif choice == "2":
            mode_code_review()
        elif choice == "3":
            mode_translate()
        elif choice == "4":
            mode_summarize()
        elif choice == "5":
            show_audit()
        elif choice == "6":
            show_prompts()
        elif choice == "":
            continue
        else:
            print(f"未知命令: {choice}，输入 :help 查看帮助")


if __name__ == "__main__":
    main()

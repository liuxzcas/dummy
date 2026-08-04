"""
============================================================
main.py — CLI 入口
============================================================

本模块是整个应用的入口点。用户运行 python main.py 从这里启动。

=== 职责 ===
1. 引导用户输入 API Key 和 Base URL（首次运行）
2. 初始化所有组件（LLM Client、Tool Registry、Agent）
3. 提供交互式 CLI 界面

=== 为什么交互式 CLI 而不是一行命令？===
对于 Phase 0 的学习目的，交互式界面可以让用户实时看到
每次 tool calling 的过程。Hermes 的 CLI 模式也是交互式的。

=== 为什么不用 argparse 处理命令行参数？===
Phase 0 保持简单，手动提示输入。
后续 Phase 可以添加：
- 支持 --api-key 和 --base-url 参数
- 支持从 .env 文件读取配置
- 支持推理模式（hermes chat -q "..."）
============================================================
"""

import sys
import os
from dotenv import load_dotenv

# -------------------------------------------------------
# 导入项目模块
# 注意：这些模块和 main.py 在同一目录下，
# Python 会自动搜索当前目录下的模块。
# 如果从其他目录运行，需要设置 PYTHONPATH。
# -------------------------------------------------------
from llm import LLMClient
from tools import create_default_registry
from core import DummyAgent


def print_banner():
    """打印启动横幅。"""
    print("""
    ╔══════════════════════════════════════════╗
    ║         Dummy Agent — Phase 0            ║
    ║    从零开始的 Tool-Calling Agent         ║
    ╚══════════════════════════════════════════╝
    """)


def print_help():
    """打印帮助信息。"""
    print("""
    可用命令:
      /help     显示此帮助
      /reset    重置对话历史
      /resume   恢复上一个持久化会话（默认恢复最新）
      /resume <session_id>  恢复指定会话
      /sessions 列出所有持久化会话
      /tools    列出可用工具
      /history  显示对话历史（调试用）
      /quit     退出 (/exit /q 也可)

    用法: 直接输入你的问题或指令，Agent 会自动决定是否调用工具。
    """)


def main():
    """
    主入口函数。

    流程：
    1. 打印横幅
    2. 获取 API Key 和 Base URL（从输入或环境变量）
    3. 初始化 LLM 客户端
    4. 创建工具注册表
    5. 创建 Agent 实例
    6. 进入交互循环
    """
    print_banner()

    # ===========================================================
    # Step 0: 从 .env 文件加载环境变量（如果有的话）
    # .env 文件在 .gitignore 中，不会提交到 GitHub
    # ===========================================================
    load_dotenv()

    # ===========================================================
    # Step 1: 获取凭证
    # ===========================================================
    # 优先级：环境变量 > 用户输入
    # 环境变量方式更安全（不小心分享代码时不会泄露 API Key）
    # 也是生产环境的推荐做法
    # ===========================================================

    # 尝试从环境变量读取 API Key（支持两个变量名，兼容不同习惯）
    api_key = os.environ.get("DUMMY_API", "").strip()
    if not api_key:
        api_key = os.environ.get("DUMMY_AGENT_API_KEY", "").strip()
    if not api_key:
        api_key = input("请输入你的 DeepSeek API Key: ").strip()
        if not api_key:
            print("错误: API Key 不能为空。")
            print("你可以设置环境变量 DUMMY_API 或 DUMMY_AGENT_API_KEY 避免每次都输入。")
            sys.exit(1)

    # 尝试从环境变量读取 Base URL
    base_url = os.environ.get("DUMMY_AGENT_BASE_URL", "").strip()
    if not base_url:
        base_url = input("请输入 API Base URL (直接回车使用 DeepSeek 官方地址): ").strip()
        if not base_url:
            base_url = "https://api.deepseek.com"

    model = "deepseek-v4-flash"  # Phase 0 固定使用此模型

    print(f"\n  模型: {model}")
    print(f"  地址: {base_url}")
    print()

    # ===========================================================
    # Step 2: 初始化组件（依赖注入）
    # ===========================================================
    # 创建顺序：首先 LLM 客户端（无依赖）
    #         然后工具注册表（无依赖）
    #         最后 Agent（依赖前两者）
    # ===========================================================
    llm = LLMClient(api_key=api_key, base_url=base_url, model=model)
    registry = create_default_registry()
    agent = DummyAgent(llm_client=llm, tool_registry=registry)

    # ===========================================================
    # Step 3: 交互循环
    # ===========================================================
    # 这是一个典型的 REPL（Read-Eval-Print Loop）模式：
    # 读取用户输入 → 交给 Agent 处理 → 打印结果 → 继续
    # ===========================================================
    print("输入 /help 查看命令列表，输入 /quit 退出。\n")

    while True:
        try:
            # 读取
            user_input = input("你 > ").strip()

            # ---------------------------------------------------
            # 处理内置命令（不经过 Agent 处理）
            # ---------------------------------------------------
            if not user_input:
                continue

            if user_input.lower() in ("/quit", "/exit", "/q"):
                print("再见！")
                break

            if user_input.lower() == "/help":
                print_help()
                continue

            if user_input.lower() == "/reset":
                agent.reset()
                print("✅ 对话历史已重置\n")
                continue

            if user_input.lower() == "/resume":
                ok = agent.resume_last_session()
                if ok:
                    restored_session_id = agent.current_session_id
                    restored_count = len(agent.get_history())
                    print(f"✅ 已恢复上一个会话: {restored_session_id}")
                    print(f"📝 当前会话历史 ({restored_count} 条消息)\n")
                else:
                    print("⚠️ 没有可恢复的历史会话。\n")
                continue

            if user_input.lower().startswith("/resume "):
                session_id = user_input.split(maxsplit=1)[1].strip()
                ok = agent.resume_session(session_id)
                if ok:
                    restored_count = len(agent.get_history())
                    print(f"✅ 已恢复会话: {agent.current_session_id}")
                    print(f"📝 当前会话历史 ({restored_count} 条消息)\n")
                else:
                    print(f"⚠️ 无法恢复会话: {session_id}\n")
                continue

            if user_input.lower() == "/sessions":
                sessions = agent.session_store.list_sessions()
                if not sessions:
                    print("🗂️ 当前没有任何持久化会话。\n")
                else:
                    print("🗂️ 持久化会话列表:")
                    for idx, session in enumerate(sessions, start=1):
                        message_count = int(session.get("message_count", 0))
                        created_at = session.get("created_at", "")
                        updated_at = session.get("updated_at", "")
                        print(
                            f"  [{idx}] {session['id']} "
                            f"| messages={message_count} "
                            f"| created={created_at} "
                            f"| updated={updated_at}"
                        )
                print()
                continue

            if user_input.lower() == "/tools":
                tool_list = registry.list_tools()
                print(f"📦 可用工具 ({len(tool_list)}):")
                for name in tool_list:
                    print(f"   - {name}")
                print()
                continue

            if user_input.lower() == "/history":
                history = agent.get_history()
                print(f"📝 对话历史 ({len(history)} 条消息):")
                for i, msg in enumerate(history):
                    role = msg["role"]
                    content_preview = (str(msg.get("content", ""))[:100]
                                       if msg.get("content") else "(tool_calls)")
                    print(f"  [{i}] {role}: {content_preview}")
                print()
                continue

            # ---------------------------------------------------
            # 交给 Agent 处理
            # ---------------------------------------------------
            print("  🤔 Agent 思考中...")
            response = agent.chat(user_input)
            print(f"\n🤖 Agent: {response}\n")

        except KeyboardInterrupt:
            # Ctrl+C 处理 —— 优雅退出
            print("\n\n再见！")
            break

        except EOFError:
            # Ctrl+D 处理（Unix 终端下）
            print("\n\n再见！")
            break

        except Exception as e:
            # 捕获并显示未预期的错误
            # 对于 Phase 0 学习项目，显示完整 traceback 更有教育意义
            print(f"\n❌ 发生错误: {type(e).__name__}: {e}")
            print("   详细信息请看上方 traceback。\n")
            # 不退出，让用户可以继续


if __name__ == "__main__":
    main()

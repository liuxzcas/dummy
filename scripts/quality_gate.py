"""Phase 2.2 Step 7 质量门:8 题事实召回,真实 DeepSeek 压缩 + 回答。

标准(phase2.2_plan.md 7.1):8 题答对 ≥ 90%(8 题即 8/8)。

流程(每题):
1. 构造 10 轮对话,事实埋在早期轮(会被 L2 摘要)
2. 真实 DeepSeek 跑压缩(ContextCompressor + LLMClient)
3. 以压缩后历史为唯一上下文,提问
4. 答案含期望子串(反斜杠归一化为斜杠)即答对

用法:python scripts/quality_gate.py
前置:环境变量 DUMMY_API(或 DUMMY_AGENT_API_KEY);本脚本发真实请求。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from llm import LLMClient  # noqa: E402
from compressor import ContextCompressor, strip_meta  # noqa: E402

SYSTEM = "你是测试助手,严格根据对话历史回答问题,只输出答案本身,不要解释。"


def build_history(fact_turns: list[list[dict]], filler_after: int = 7) -> list[dict]:
    """构造 10 轮对话:事实轮在前(被摘要),填充轮在后,无内置提问。

    填充轮刻意中性(不能含"任务/下一步/计划"等与提问易混淆的措辞,
    否则模型会被保留区的最近模式带偏——那是测量噪音,不是压缩质量问题)。
    """
    h = [{"role": "system", "content": SYSTEM}]
    for i, turn in enumerate(fact_turns):
        h += [{"role": "user", "content": turn[0]},
              {"role": "assistant", "content": turn[1]}]
    for i in range(filler_after):
        h += [{"role": "user", "content": f"补充说明第{i}点"},
              {"role": "assistant", "content": "好的"}]
    return h


def build_tool_history() -> list[dict]:
    """第 7 题:2000 字符日志,错误信息在尾部(L1 折叠保留尾 100)。"""
    log = "L" * 1900 + "\nERROR: port 8080"
    h = [{"role": "system", "content": SYSTEM}]
    h += [{"role": "user", "content": "看一下运行日志"}, {"role": "assistant", "content": "好的"}]
    h += [{"role": "user", "content": "读日志文件"}, {"role": "assistant", "content": None,
          "tool_calls": [{"id": "clog", "type": "function",
                          "function": {"name": "read_file", "arguments": "{}"}}]}]
    h += [{"role": "tool", "tool_call_id": "clog", "content": log},
          {"role": "assistant", "content": "日志已读取"}]
    for i in range(6):
        h += [{"role": "user", "content": f"继续任务{i}"},
              {"role": "assistant", "content": "好的"}]
    return h


CASES = [
    ("1 文件路径",
     build_history([["把结果写到 D:/Engineering/dummy/output.txt", "记住了"]]),
     "输出文件的完整路径是什么?", ["D:/Engineering/dummy/output.txt"]),
    ("2 代码细节",
     build_history([["用 requests 库,超时 30 秒", "好的"]], filler_after=8),
     "用的是什么库?超时时间是多少秒?", ["requests", "30"]),
    ("3 用户偏好",
     build_history([["以后回复都用中文", "好的,以后用中文"]], filler_after=8),
     "用户要求用什么语言回复?", ["中文"]),
    ("4 决定",
     build_history([["决定先做 ToolResult 折叠", "收到"]], filler_after=8),
     "决定先做什么?", ["ToolResult 折叠"]),
    ("5 数字",
     build_history([["预算 500 元", "明白"]], filler_after=8),
     "预算金额是多少?", ["500"]),
    ("6 未完成事项",
     build_history([["我的下一步计划是写测试", "记下了"]]),
     "用户的下一步计划是什么?", ["写测试"]),
    ("7 工具结果", build_tool_history(),
     "日志中的错误信息是什么?", ["port 8080"]),
    ("8 跨轮次",
     build_history([["项目名是 dummy-agent", "知道了"]], filler_after=8),
     "项目名是什么?", ["dummy-agent"]),
]


def norm(s: str) -> str:
    """归一化:小写 + 反斜杠转正斜杠 + 去掉所有空白。

    空白差异(如 "ToolResult 折叠" vs "toolresult折叠")不是质量失败,
    是测量噪音,必须归一化掉。
    """
    return re.sub(r"\s+", "", (s or "").strip().lower().replace("\\", "/"))


def main() -> int:
    api_key = os.environ.get("DUMMY_API") or os.environ.get("DUMMY_AGENT_API_KEY", "")
    if not api_key:
        print("缺少 API key:请设置环境变量 DUMMY_API")
        return 2
    base_url = os.environ.get("DUMMY_AGENT_BASE_URL", "").strip()
    # 注意:不能传 None(会覆盖 LLMClient 默认的 DeepSeek 地址,导致打到 OpenAI)
    llm = LLMClient(api_key=api_key, base_url=base_url, model="deepseek-chat") if base_url \
        else LLMClient(api_key=api_key, model="deepseek-chat")
    comp = ContextCompressor(llm)

    passed = 0
    total_tokens = 0
    print(f"{'题目':<14}{'结果':<6}回答摘录")
    for name, history, question, expected in CASES:
        result = comp.compress(history)
        context = result.history if result.success else history  # 失败/降级按 agent 实际行为
        degrade = "降级" if (result.success and result.error_type) else ("失败" if not result.success else "")
        msgs = strip_meta(context) + [{"role": "user", "content": question}]
        resp = llm.chat(messages=msgs, temperature=0.2, max_tokens=300)
        answer = norm(resp.content or "")
        if llm.last_usage:
            total_tokens += llm.last_usage.total_tokens
        ok = all(e in answer for e in (norm(e) for e in expected))
        passed += ok
        snippet = (answer[:40] + "…") if len(answer) > 40 else answer
        print(f"{name:<14}{'✅' if ok else '❌':<6}{snippet}" + (f"  [{degrade}]" if degrade else ""))

    score = passed / len(CASES)
    print(f"\n得分:{passed}/{len(CASES)} = {score:.0%} | 质量门 ≥90% → {'通过' if score >= 0.9 else '不通过'}")
    print(f"本次调用总 tokens ≈ {total_tokens}")
    return 0 if score >= 0.9 else 1


if __name__ == "__main__":
    sys.exit(main())

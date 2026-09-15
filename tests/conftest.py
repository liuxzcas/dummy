"""测试公共设施:项目模块导入路径 + 假 LLM 驱动真实工具循环的夹具。

为什么要有假 LLM:
工具循环的行为(护栏、验证门、历史配对)只有在**真实循环**里才能被证伪,
但真实 API 既花钱又不可重复。所以用一个按脚本返回 tool_calls 的假 LLM 驱动
core.chat(),把"循环/接线"这件事变成可断言的确定性事实。

区分主循环与旁路调用靠 tools 参数:主循环会传 tools=工具定义,旁路调用
(记忆抽取、教训生成、压缩摘要)不传。这样假 LLM 只在主循环里吐 tool_calls。
"""

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
from tools import create_default_registry  # noqa: E402


class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _TC:
    def __init__(self, i, name, arguments):
        self.id, self.type = f"call_{i}", "function"
        self.function = _Fn(name, arguments)


class _Msg:
    """模仿 OpenAI SDK 的返回消息(需要 to_dict,core 会调它落库)。"""

    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls or []

    def to_dict(self):
        data = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [
                {"id": t.id, "type": "function",
                 "function": {"name": t.function.name,
                              "arguments": t.function.arguments}}
                for t in self.tool_calls
            ]
        return data


class _Usage:
    prompt_tokens = 100
    completion_tokens = 10
    cached_tokens = 0


class ScriptedLLM:
    """按脚本返回 tool_calls 的假 LLM;脚本用尽后一律返回纯文本。

    - 主循环调用(带 tools)按脚本吐 tool_calls;
    - 旁路调用(不带 tools:记忆抽取/教训生成/压缩摘要/轮次用尽后的收尾总结)
      一律返回 bypass_text;
    - raise_on_bypass=True 时旁路调用直接抛异常(用于测"兜底不崩")。
    """

    def __init__(self, script=(), final_text="(fake) 结束。",
                 bypass_text="(旁路) ok", raise_on_bypass=False):
        self.script, self.final_text = list(script), final_text
        self.bypass_text, self.raise_on_bypass = bypass_text, raise_on_bypass
        self.i = 0
        self.last_usage, self.last_reasoning = _Usage(), None
        self.main_calls = 0
        self.bypass_calls = 0
        self.seen_messages: list[list[dict]] = []

    def get_model_name(self):
        return "fake"

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=4096):
        self.seen_messages.append(list(messages))
        if not tools:                      # 旁路调用
            self.bypass_calls += 1
            if self.raise_on_bypass:
                raise RuntimeError("bypass call failed (模拟旁路调用失败)")
            return _Msg(content=self.bypass_text)
        self.main_calls += 1
        if self.i < len(self.script):
            name, args = self.script[self.i]
            self.i += 1
            return _Msg(tool_calls=[_TC(self.i, name, json.dumps(args))])
        return _Msg(content=self.final_text)


@pytest.fixture()
def run_script(tmp_path, monkeypatch):
    """在隔离的 session 库里跑一遍脚本,返回 (agent, llm)。

    隔离两点:会话库指向 tmp(不碰真实 session.db);
    确认提问用桩自动放行(真机是人工确认,测试里排除这个变量)。
    """
    real_store = core.SessionStore
    monkeypatch.setattr(
        core, "SessionStore",
        lambda *a, **k: real_store(db_path=str(tmp_path / "t.db")))

    def _run(script=(), confirm="y", llm=None):
        # 危险写法拦截:run_script 会**真跑**工具。脚本里若让 terminal 真跑
        # pytest,而套件内部又要跑 pytest,就会递归套娃 —— 实测一次误写跑出
        # 101 个残留 pytest 进程(约 9GB),把系统资源耗尽,连别的测试都起不来。
        # 要么用 --collect-only(见 test_verify_stop 的 _COLLECT),要么自己建
        # agent 并替换 tools.dispatch(见 test_guardrails 的两条 e2e)。
        for name, args in script:
            cmd = str((args or {}).get("command", "")) if name == "terminal" else ""
            if cmd and re.search(r"pytest(?!.*--collect-only)", cmd):
                raise AssertionError(
                    "run_script 的脚本里不能真跑 pytest(会递归套娃炸进程): "
                    f"{cmd!r}\n改用 '--collect-only',或自建 agent 并替换 "
                    "tools.dispatch。")
        llm = llm or ScriptedLLM(script)
        agent = core.DummyAgent(llm, create_default_registry())
        agent.tools.set_confirm_provider(lambda prompt: confirm)
        agent.chat("开始")
        return agent, llm

    return _run


# ---------------------- 断言辅助 ----------------------
def tool_msgs(history):
    return [m for m in history if m.get("role") == "tool"]


def tool_contents(history):
    return [str(m.get("content")) for m in tool_msgs(history)]


def pairing_ok(history):
    """assistant(tool_calls) 的声明与紧随其后的 tool 回复必须一一对应。"""
    i, n = 0, len(history)
    while i < n:
        msg = history[i]
        if msg.get("role") == "tool":
            return False, f"第{i}条是孤儿 tool"
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            want = [t.get("id") for t in msg["tool_calls"]]
            j, got = i + 1, []
            while j < n and history[j].get("role") == "tool":
                got.append(history[j].get("tool_call_id"))
                j += 1
            if want != got:
                return False, f"第{i}条配对不符 declared={want} got={got}"
            i = j
            continue
        i += 1
    return True, ""

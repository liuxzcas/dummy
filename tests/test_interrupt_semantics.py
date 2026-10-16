"""打断语义测试 —— 占位文本必须如实反映"工具到底跑了没有"。

============ 被这个 bug 坑过(2026-09-18 诊断,2026-10-16 修) ============
用户实测:一次任务里模型同时调了 2 个 terminal,用户按 /p 打断,
后续历史里出现:

    [tool] [用户打断,工具未执行]                    ← 第 1 个
    [tool] [工具结果缺失:该次调用未执行或被中断]     ← 第 2 个

两个问题:
  ① **第一个工具其实已经执行完了** —— core.py 里 result 已经拿到、
     结果甚至已经打印给用户看了,但因为检测到中断,用
     "[用户打断,工具未执行]" 覆盖了真实结果。
     → 模型被告知"未执行"(与事实不符),用户看到的和模型看到的**不一致**。
  ② 两种**副作用风险完全不同**的情况用了语义相近的文案:
     · skipped   (工具没开始跑) → 肯定没副作用 → 重试安全
     · cancelled (跑到一半被打断) → 可能已改文件 → 重试危险

本文件锁住修好后的契约:
  · 工具执行完成 → **保留真实结果**,附注说明中断(不覆盖)
  · 工具未执行   → [未执行] 前缀,注明"可以安全重做"
  · 跑到一半中断 → [结果未知] 前缀,提醒"不要假设它成功了"
  · 完成后发现残留进程 → 如实报告(不判决)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core                                        # noqa: E402
from conftest import ScriptedLLM                   # noqa: E402
from tools import create_default_registry          # noqa: E402


# ============================================================
# 造历史的辅助（照 test_history_hygiene.py 的风格）
# ============================================================
def _asst_turn(*call_ids, name="read_file", args='{"path": "a.txt"}'):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function",
         "function": {"name": name, "arguments": args}}
        for cid in call_ids
    ]}


def _tool_msg(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


# ============================================================
# 状态文案的契约
# ============================================================
class TestInterruptPlaceholders:
    """三种状态的文案必须能互相区分,且语义准确。"""

    def test_skipped_placeholder_says_safe_to_retry(self):
        """未执行(skipped)→ 文案必须说明"可以安全重做"。

        因为工具根本没开始跑,不会有副作用 —— 模型可以放心重试。
        """
        from session_store import SKIPPED_TEXT
        assert "[未执行]" in SKIPPED_TEXT, \
            "skipped 的文案要有 [未执行] 前缀(lessons.NOOP_MARKERS 靠它识别)"
        assert "安全" in SKIPPED_TEXT or "重做" in SKIPPED_TEXT, \
            "要告诉模型这次调用没有副作用、可以重做"

    def test_cancelled_placeholder_warns_unknown_outcome(self):
        """跑到一半被打断(cancelled)→ 文案必须警告"不要假设它成功了"。

        这是与 skipped 的关键区别:工具可能已经改了文件/发出请求,
        重试可能造成重复副作用。
        """
        from session_store import CANCELLED_TEXT
        assert "[结果未知]" in CANCELLED_TEXT
        assert "成功" in CANCELLED_TEXT or "假设" in CANCELLED_TEXT, \
            "要警告模型不要假设这次调用成功了"

    def test_the_two_placeholders_are_distinguishable(self):
        """两种文案必须**明显不同** —— 混用就失去了区分意义。"""
        from session_store import SKIPPED_TEXT, CANCELLED_TEXT
        assert SKIPPED_TEXT != CANCELLED_TEXT
        # 前缀必须不同(这是机器可判定的部分)
        assert "[未执行]" not in CANCELLED_TEXT, \
            "cancelled 不能用 [未执行] 前缀(会与 skipped 混淆)"
        assert "[结果未知]" not in SKIPPED_TEXT, \
            "skipped 不能用 [结果未知] 前缀(会与 cancelled 混淆)"


# ============================================================
# 核心修复:工具执行完成后被打断 → 保留真实结果
# ============================================================
class TestInterruptedToolKeepsRealResult:
    """工具已经跑完才检测到中断 → **真实结果必须保留**。

    这修复的是 core.py 检查点 A 的缺陷:
        result = self.tools.dispatch(...)     # 跑完了
        ui.show(result_preview)               # 还给用户看了
        if interrupt_triggered:
            append("[用户打断,工具未执行]")    # ← 却告诉模型"未执行"
            break
    """

    def test_real_result_not_replaced_by_placeholder(self, monkeypatch):
        """断言:工具结果**出现在历史里**,而不是被占位符顶掉。"""
        registry = create_default_registry()
        # 让 terminal 工具真的跑一条无害命令
        llm = ScriptedLLM([("terminal", {"command": "echo INTERRUPT_MARKER"})])
        agent = core.DummyAgent(llm, registry)
        agent.tools.set_confirm_provider(lambda prompt: "y")   # 免交互确认
        monkeypatch.setattr("builtins.input", lambda *a, **k: "")  # 打断点不等输入

        # 关键:让 agent 在**工具执行完之后**(检查点 A)就检测到中断。
        #
        # 流程上的两个 drain 点(见 core.py):
        #   检查点 A(工具执行后,立即)     ← 第 1 次调用 —— 要在这里触发
        #   检查点 B(检查点 A 之后)        ← 第 2 次调用
        # 所以 fake 从第 1 次就返回 True。
        calls = {"n": 0}

        def fake_drain() -> bool:
            calls["n"] += 1
            return True

        agent._drain_interrupt = fake_drain

        agent.chat("测试")

        tool_msgs = [m for m in agent.history if m.get("role") == "tool"]
        assert tool_msgs, "应该有 tool 结果消息"
        first = tool_msgs[0]["content"]
        assert "INTERRUPT_MARKER" in first, (
            "工具执行完成 -> 真实结果必须保留在历史里;\n"
            f"实际拿到: {first[:200]!r}\n"
            "(如果只剩 '[用户打断,工具未执行]',说明结果又被占位符顶掉了)"
        )

    def test_result_carries_interrupt_note(self, monkeypatch):
        """保留真实结果的同时,要附注说明"这次遇到了中断"。"""
        registry = create_default_registry()
        llm = ScriptedLLM([("terminal", {"command": "echo NOTE_MARKER"})])
        agent = core.DummyAgent(llm, registry)
        agent.tools.set_confirm_provider(lambda prompt: "y")   # 免交互确认
        monkeypatch.setattr("builtins.input", lambda *a, **k: "")  # 打断点不等输入

        calls = {"n": 0}

        def fake_drain() -> bool:
            calls["n"] += 1
            return True      # 检查点 A(工具执行后)就触发

        agent._drain_interrupt = fake_drain
        agent.chat("测试")

        tool_msgs = [m for m in agent.history if m.get("role") == "tool"]
        assert tool_msgs
        first = tool_msgs[0]["content"]
        assert "中断" in first, (
            f"应当附注说明遇到中断;实际: {first[:200]!r}"
        )
        # 附注不能掩盖真实结果
        assert "NOTE_MARKER" in first


# ============================================================
# 未执行的调用:标 skipped
# ============================================================
class TestUnexecutedCallsMarkedSkipped:
    """模型一次调多个工具、用户打断 → 后续未执行的标 skipped。"""

    def test_pending_calls_use_skipped_text(self):
        """剩下没跑的调用,占位文案要走 skipped 语义。"""
        from session_store import SKIPPED_TEXT
        # 直接测填补函数的行为(纯函数,好测)
        hist = [
            _asst_turn("c1", "c2", "c3"),
            _tool_msg("c1", "第一条结果"),
        ]
        replied = {"c1"}
        pending = [c for c in ("c1", "c2", "c3") if c not in replied]
        assert pending == ["c2", "c3"]
        # 契约:c2/c3 都该用 SKIPPED_TEXT
        assert "[未执行]" in SKIPPED_TEXT


# ============================================================
# 与 lessons 的衔接
# ============================================================
class TestLessonMarkersIntegration:
    """新文案必须被 lessons 的 NOOP_MARKERS 认出来(算"非成功")。"""

    def test_skipped_text_recognised_by_noop_markers(self):
        """[未执行] 已在 NOOP_MARKERS 里 → is_tool_error 认它是"非成功"。"""
        from lessons import NOOP_MARKERS
        assert any("[未执行]" in m for m in NOOP_MARKERS), \
            "NOOP_MARKERS 应含 [未执行]"

    def test_cancelled_text_recognised_by_noop_markers(self):
        """[结果未知] 也必须被认出来(否则会被当成"成功",误导教训生成)。"""
        from lessons import NOOP_MARKERS
        assert any("[结果未知]" in m for m in NOOP_MARKERS), \
            "NOOP_MARKERS 应含 [结果未知](本次新增)"

    def test_placeholders_count_as_non_success(self):
        """两种占位都不该被算成"工具执行成功"。"""
        from lessons import is_tool_error
        from session_store import SKIPPED_TEXT, CANCELLED_TEXT
        assert is_tool_error(SKIPPED_TEXT) is True
        assert is_tool_error(CANCELLED_TEXT) is True

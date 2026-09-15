"""P2a 预算模型测试集:轮次上限可配置 + 用尽后剥工具写收尾总结。

旧行为:轮次用尽 → 只追加一句 "[已达最大工具调用轮次 40,停止循环]" 就返回,
几十轮的工具成果没有任何交代。
新行为:剥掉工具定义再放一次调用,让模型用已有信息写"已完成/卡点/遗留文件/
下一步";这次调用失败或空 → 退回旧提示文本(不让收尾本身变成新故障点)。

注:轮次"退票"(refund)未实现——dummy 没有 execute_code 这类"天生烧轮次"的
工具,退票没有接收者,加了就是死代码。见提交说明。
"""

import core
from conftest import ScriptedLLM
from tools import create_default_registry

SUMMARY_MARK = "[系统: 本轮可用的调用轮次已用尽"


# ============================================================
# 轮次上限可配置
# ============================================================
def test_env_int_semantics(monkeypatch):
    """_env_int:未设置/非法/<1 一律退回默认。"""
    monkeypatch.delenv("X_TURNS", raising=False)
    assert core._env_int("X_TURNS", 40) == 40

    monkeypatch.setenv("X_TURNS", "7")
    assert core._env_int("X_TURNS", 40) == 7

    monkeypatch.setenv("X_TURNS", "abc")
    assert core._env_int("X_TURNS", 40) == 40

    monkeypatch.setenv("X_TURNS", "0")
    assert core._env_int("X_TURNS", 40) == 40, "<1 视为非法"

    monkeypatch.setenv("X_TURNS", "-3")
    assert core._env_int("X_TURNS", 40) == 40


def test_env_override(monkeypatch, run_script):
    """DUMMY_MAX_TOOL_TURNS 可覆盖默认值。"""
    monkeypatch.setenv("DUMMY_MAX_TOOL_TURNS", "3")
    agent, _ = run_script(())
    assert agent.max_tool_turns == 3


def test_default_is_max_tool_turns(monkeypatch, run_script):
    monkeypatch.delenv("DUMMY_MAX_TOOL_TURNS", raising=False)
    agent, _ = run_script(())
    assert agent.max_tool_turns == core.DummyAgent.MAX_TOOL_TURNS == 40


# ============================================================
# 轮次用尽 → 剥工具写总结
# ============================================================
def _run_exhausted(tmp_path, monkeypatch, llm):
    """把上限压到 1 轮跑一次;返回 (agent, 返回值)。"""
    real = core.SessionStore
    monkeypatch.setattr(core, "SessionStore",
                        lambda *a, **k: real(db_path=str(tmp_path / "b.db")))

    target = str(tmp_path / "f.txt")
    (tmp_path / "f.txt").write_text("内容\n", encoding="utf-8")

    agent = core.DummyAgent(llm, create_default_registry())
    agent.tools.set_confirm_provider(lambda prompt: "y")
    agent.max_tool_turns = 1
    return agent, agent.chat("开始")


def _one_read_script(tmp_path):
    return [("read_file", {"path": str(tmp_path / "f.txt")})]


def test_summary_call_has_no_tools(tmp_path, monkeypatch):
    """用尽后剥掉工具再放一次调用,返回值是总结而不是那句硬停提示。"""
    llm = ScriptedLLM(bypass_text="总结正文")
    llm.script = _one_read_script(tmp_path)
    agent, result = _run_exhausted(tmp_path, monkeypatch, llm)

    assert llm.bypass_calls >= 1, "应发生一次无工具的收尾调用"
    assert result == "总结正文", "返回值应是收尾总结,而非硬停提示"
    assert any(SUMMARY_MARK in str(m.get("content")) for m in agent.history), \
        "收尾指令应写进历史"
    assert llm.main_calls == 1, "主循环只跑 1 轮,之后是收尾调用"


def test_history_records_fallback_and_summary(tmp_path, monkeypatch):
    """历史里既有超限提示,也有随后生成的总结(便于下次 resume 看清怎么结束的)。"""
    llm = ScriptedLLM(bypass_text="总结正文")
    llm.script = _one_read_script(tmp_path)
    agent, _ = _run_exhausted(tmp_path, monkeypatch, llm)

    texts = [str(m.get("content")) for m in agent.history if m.get("role") == "assistant"]
    assert any("已达最大工具调用轮次 1" in t for t in texts), "应记录超限提示"
    assert "总结正文" in texts, "应记录收尾总结"


def test_summary_failure_falls_back_gracefully(tmp_path, monkeypatch):
    """收尾调用抛异常 → 退回旧提示文本,不把收尾变成新的崩溃点。"""
    llm = ScriptedLLM(raise_on_bypass=True)
    llm.script = _one_read_script(tmp_path)
    _, result = _run_exhausted(tmp_path, monkeypatch, llm)   # 不应抛异常

    assert "已达最大工具调用轮次 1" in result


def test_summary_empty_text_falls_back(tmp_path, monkeypatch):
    """收尾调用返回空 → 同样退回旧提示文本。"""
    llm = ScriptedLLM(bypass_text="   ")
    llm.script = _one_read_script(tmp_path)
    _, result = _run_exhausted(tmp_path, monkeypatch, llm)

    assert "已达最大工具调用轮次 1" in result


def test_pairing_valid_after_budget_exhausted(tmp_path, monkeypatch):
    """收尾路径不破坏 tool 配对约束。"""
    from conftest import pairing_ok

    llm = ScriptedLLM()
    llm.script = _one_read_script(tmp_path)
    agent, _ = _run_exhausted(tmp_path, monkeypatch, llm)

    ok, why = pairing_ok(agent.history)
    assert ok, why

"""历史卫生测试集:tool 配对自愈 + 中断序列闭合。

覆盖两个纯函数(都在 session_store.py):
  - repair_tool_pairing          补缺失 / 删孤儿 / 去重 / 删尾部悬空块
  - close_interrupted_tool_sequence  尾巴停在裸 tool 上时补合成 assistant 轮次

这两个函数是"一次损坏、永久失效"类故障的唯一防线
(400 insufficient tool messages / tool → user 角色交替违规),
此前只有临时脚本验证过,本文件把它固定下来。
"""

import os

import core
from conftest import ScriptedLLM
from session_store import (
    INTERRUPT_CLOSE_TEXT,
    SessionStore,
    close_interrupted_tool_sequence,
    repair_tool_pairing,
)
from tools import create_default_registry


# ============================================================
# 造历史的辅助
# ============================================================
def _asst(call_ids, text=None):
    return {"role": "assistant", "content": text, "tool_calls": [
        {"id": cid, "type": "function",
         "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}
        for cid in call_ids
    ]}


def _tool(cid, content="结果"):
    return {"role": "tool", "tool_call_id": cid, "content": content}


def _legal():
    """完全合法的历史:声明 2 个,回复 2 个。"""
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "帮我看看"},
        _asst(["c1", "c2"]),
        _tool("c1"),
        _tool("c2"),
        {"role": "assistant", "content": "看完了"},
    ]


# ============================================================
# repair_tool_pairing
# ============================================================
def test_legal_history_untouched():
    """合法历史零改动(幂等)。"""
    hist = _legal()
    fixed, stats = repair_tool_pairing(hist)
    assert fixed == hist
    assert stats["total"] == 0


def test_missing_reply_backfilled():
    """声明 2 个只回复 1 个 → 补 1 条占位,顺序与声明一致。"""
    hist = [{"role": "user", "content": "u"}, _asst(["c1", "c2"]), _tool("c1")]
    fixed, stats = repair_tool_pairing(hist)
    assert stats["backfilled"] == 1
    assert stats["dropped_dangling"] == 0
    ids = [m.get("tool_call_id") for m in fixed if m.get("role") == "tool"]
    assert ids == ["c1", "c2"]


def test_partial_tail_is_backfilled_not_dropped():
    """部分回复的尾部块 → 补,不删(工具循环真在进行中,要让它跑完)。"""
    hist = [{"role": "user", "content": "u"}, _asst(["c1", "c2"]), _tool("c1")]
    fixed, stats = repair_tool_pairing(hist)
    assert stats["dropped_dangling"] == 0
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in fixed)


def test_dangling_zero_reply_tail_is_dropped():
    """尾部零回复块 → 整块删除(这次调用从来没发生过)。"""
    hist = [{"role": "user", "content": "u"}, _asst(["c1", "c2"])]
    fixed, stats = repair_tool_pairing(hist)
    assert stats["dropped_dangling"] == 1
    assert stats["backfilled"] == 0, "已删除,不该再补占位"
    assert not any(m.get("tool_calls") for m in fixed)
    assert fixed == [{"role": "user", "content": "u"}]


def test_zero_reply_not_at_tail_is_backfilled():
    """非尾部的零回复块 → 走"补"(边界:只删尾巴)。"""
    hist = [
        {"role": "user", "content": "u"},
        _asst(["c1"]),
        {"role": "assistant", "content": "继续"},
        {"role": "user", "content": "再问"},
    ]
    fixed, stats = repair_tool_pairing(hist)
    assert stats["dropped_dangling"] == 0
    assert stats["backfilled"] == 1


def test_orphan_tool_dropped():
    """没有归属的孤儿 tool 消息 → 丢弃。"""
    hist = [{"role": "user", "content": "u"}, _tool("ghost")]
    fixed, stats = repair_tool_pairing(hist)
    assert stats["dropped_orphans"] == 1
    assert fixed == [{"role": "user", "content": "u"}]


def test_duplicate_reply_deduped():
    """同一 tool_call_id 回复多次 → 只留第一条。"""
    hist = [{"role": "user", "content": "u"}, _asst(["c1"]),
            _tool("c1", "第一次"), _tool("c1", "重复")]
    fixed, stats = repair_tool_pairing(hist)
    assert stats["dropped_duplicates"] == 1
    tools = [m for m in fixed if m.get("role") == "tool"]
    assert len(tools) == 1 and tools[0]["content"] == "第一次"


def test_empty_history():
    assert repair_tool_pairing([]) == ([], {
        "dropped_dangling": 0, "backfilled": 0,
        "dropped_orphans": 0, "dropped_duplicates": 0, "total": 0})


def test_does_not_mutate_input():
    """纯函数:不改输入列表。"""
    hist = [{"role": "user", "content": "u"}, _asst(["c1"])]
    snapshot = list(hist)
    repair_tool_pairing(hist)
    assert hist == snapshot


def test_idempotent_on_repaired_history():
    """对已修好的历史再修一次 → 零改动。"""
    hist = [{"role": "user", "content": "u"}, _asst(["c1", "c2"]), _tool("c1")]
    once, _ = repair_tool_pairing(hist)
    twice, stats = repair_tool_pairing(once)
    assert twice == once and stats["total"] == 0


# ============================================================
# close_interrupted_tool_sequence
# ============================================================
def test_close_appends_synthetic_assistant_after_tool():
    """尾巴是裸 tool → 补一条合成 assistant,闭合这一轮。"""
    hist = [{"role": "user", "content": "u"}, _asst(["c1"]), _tool("c1")]
    fixed, changed = close_interrupted_tool_sequence(hist)
    assert changed is True
    assert fixed[-1] == {"role": "assistant", "content": INTERRUPT_CLOSE_TEXT}


def test_close_noop_for_other_tails():
    """尾巴不是 tool → 原样返回(中途尾巴是 tool 才需要收尾)。"""
    for hist in (
        [{"role": "user", "content": "u"}],
        [{"role": "user", "content": "u"}, {"role": "assistant", "content": "答"}],
        [],
    ):
        fixed, changed = close_interrupted_tool_sequence(hist)
        assert changed is False and fixed == hist


def test_close_is_idempotent():
    """补过一次之后再调用 → 不再补。"""
    hist = [{"role": "user", "content": "u"}, _asst(["c1"]), _tool("c1")]
    once, _ = close_interrupted_tool_sequence(hist)
    twice, changed = close_interrupted_tool_sequence(once)
    assert changed is False and twice == once


def test_close_does_not_mutate_input():
    hist = [{"role": "user", "content": "u"}, _asst(["c1"]), _tool("c1")]
    snapshot = list(hist)
    close_interrupted_tool_sequence(hist)
    assert hist == snapshot


# ============================================================
# 端到端:resume 之后开新一轮
# ============================================================
def _bad_adjacency(history):
    """返回所有 `tool 紧跟 user` 的位置(角色交替违规)。"""
    return [i for i in range(len(history) - 1)
            if history[i].get("role") == "tool" and history[i + 1].get("role") == "user"]


def _agent(tmp_path, monkeypatch):
    real = core.SessionStore
    monkeypatch.setattr(core, "SessionStore",
                        lambda *a, **k: real(db_path=str(tmp_path / "h.db")))
    agent = core.DummyAgent(ScriptedLLM(), create_default_registry())
    agent.tools.set_confirm_provider(lambda prompt: "y")
    return agent


def test_e2e_dangling_tail_dropped_on_resume(tmp_path, monkeypatch):
    """库里存着"零回复的悬空尾"→ resume 时被删掉,不会诱导模型重放。"""
    agent = _agent(tmp_path, monkeypatch)
    store = agent.session_store
    sid = store.create_session()
    store.save_history(sid, [{"role": "user", "content": "u"},
                             _asst(["c1", "c2"])])

    assert agent.resume_session(sid) is True
    assert not any(m.get("tool_calls") for m in agent.history), "悬空尾应已删除"


def test_e2e_new_turn_after_trailing_tool_has_no_adjacency_violation(tmp_path, monkeypatch):
    """库里停在裸 tool 上 → 开新一轮时会闭合,不产生 `tool → user`。"""
    agent = _agent(tmp_path, monkeypatch)
    store = agent.session_store
    sid = store.create_session()
    store.save_history(sid, [{"role": "user", "content": "u"},
                             _asst(["c1"]), _tool("c1")])

    assert agent.resume_session(sid) is True
    agent.chat("继续")

    assert _bad_adjacency(agent.history) == []
    assert any(m.get("content") == INTERRUPT_CLOSE_TEXT for m in agent.history)

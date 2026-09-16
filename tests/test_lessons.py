"""
tests/test_lessons.py — Phase 3 Step 3:错误学习(教训机制)

覆盖:
1. lessons 表 CRUD:add/list/delete/confirm/hits 自动转正
2. lessons.py:纠正信号检测 / 工具错误特征 / 反思生成(三级解析)
3. core._inject_lessons:按需检索注入 ≤5、幂等、保底
4. core._learn_from_correction / _learn_from_tool_error:触发与限流
5. /lessons 命令:list/confirm/del
6. delete_session 级联删 lessons
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import lessons as L  # noqa: E402
import main as mm  # noqa: E402
from core import DummyAgent  # noqa: E402
from session_store import SessionStore  # noqa: E402
from tools import create_default_registry  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    return SessionStore(str(tmp_path / "m.db"))


@pytest.fixture()
def sid(store):
    return store.create_session()


class FakeLLM:
    """按调用顺序返回预设响应。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs.get("messages"))
        return self._responses.pop(0)


# ---------------------------------------------------------------
# lessons 表 CRUD
# ---------------------------------------------------------------
def test_lesson_crud(store, sid):
    lid = store.add_lesson(sid, "Windows 上别用 cmd 语法,用 bash", "terminal")
    assert lid > 0
    rows = store.list_lessons()
    assert len(rows) == 1
    assert rows[0]["lesson"].startswith("Windows")
    assert rows[0]["status"] == "pending"
    # confirm
    assert store.confirm_lesson(lid) is True
    assert store.list_lessons()[0]["status"] == "verified"
    assert store.confirm_lesson(9999) is False
    # delete
    assert store.delete_lesson(lid) is True
    assert store.list_lessons() == []
    assert store.delete_lesson(lid) is False


def test_lesson_hits_auto_verify(store, sid):
    lid = store.add_lesson(sid, "规则A", "general")
    store.increment_lesson_hits([lid])
    assert store.list_lessons()[0]["status"] == "pending"
    store.increment_lesson_hits([lid])
    assert store.list_lessons()[0]["status"] == "verified"


def test_lesson_status_filter(store, sid):
    store.add_lesson(sid, "待验证", "general")
    v = store.add_lesson(sid, "已验证", "general")
    store.confirm_lesson(v)
    assert len(store.list_lessons(status="verified")) == 1
    assert len(store.list_lessons(status="pending")) == 1


def test_delete_session_cascades_lessons(store, sid):
    store.add_lesson(sid, "规则", "general")
    assert store.delete_session(sid) is True
    assert store.list_lessons() == []


# ---------------------------------------------------------------
# 会话占用锁(多进程防双开)
# ---------------------------------------------------------------
def test_session_lock_acquire_release(store, sid):
    ok, msg = store.acquire_session_lock(sid, "pid:1")
    assert ok is True
    # 同一 owner 幂等
    ok, msg = store.acquire_session_lock(sid, "pid:1")
    assert ok is True and "本进程" in msg
    # 他人持有被拒
    ok, msg = store.acquire_session_lock(sid, "pid:2")
    assert ok is False and "持有" in msg
    # owner 匹配释放
    store.release_session_lock(sid, "pid:2")  # 错误 owner 不释放
    assert store.get_session_lock(sid)["locked_by"] == "pid:1"
    store.release_session_lock(sid, "pid:1")
    assert store.get_session_lock(sid)["locked_by"] is None


def test_session_lock_ttl_expired_takeover(store, sid):
    """锁 TTL 过期视为残留,自动接管。"""
    store.acquire_session_lock(sid, "pid:1")
    # 把 locked_at 改到 TTL 之前
    import sqlite3
    conn = sqlite3.connect(store.db_path)
    conn.execute("UPDATE sessions SET locked_at = ? WHERE id = ?",
                 ("2020-01-01T00:00:00+00:00", sid))
    conn.commit()
    conn.close()
    ok, msg = store.acquire_session_lock(sid, "pid:2")
    assert ok is True  # 过期锁接管
    assert store.get_session_lock(sid)["locked_by"] == "pid:2"


def test_session_lock_resume_warn(store, sid, tmp_path, monkeypatch):
    """resume 被占用会话:提示并可选接管。"""
    store.save_history(sid, [{"role": "system", "content": "s"}])
    store.acquire_session_lock(sid, "pid:999")
    from core import DummyAgent
    from tools import create_default_registry
    agent = DummyAgent(None, create_default_registry(), system_prompt="P")
    agent.session_store = store
    # 用户拒绝接管
    monkeypatch.setattr("builtins.input", lambda p: "n")
    assert agent.resume_session(sid) is False
    assert agent.current_session_id is None
    # 用户强制接管
    monkeypatch.setattr("builtins.input", lambda p: "y")
    assert agent.resume_session(sid) is True
    assert agent.current_session_id == sid
    assert store.get_session_lock(sid)["locked_by"] == agent._session_owner


def test_release_lock_on_exit(store, sid, tmp_path):
    """退出释放:main._release_current_session_lock 释放当前会话锁。"""
    from core import DummyAgent
    from tools import create_default_registry
    agent = DummyAgent(None, create_default_registry(), system_prompt="P")
    agent.session_store = store
    agent.current_session_id = sid
    store.acquire_session_lock(sid, agent._session_owner)
    assert store.get_session_lock(sid)["locked_by"] == agent._session_owner
    mm._release_current_session_lock(agent)
    assert store.get_session_lock(sid)["locked_by"] is None
    # 无会话时不崩
    agent2 = DummyAgent(None, create_default_registry(), system_prompt="P")
    agent2.session_store = store
    mm._release_current_session_lock(agent2)


# ---------------------------------------------------------------
# lessons.py:信号检测与反思生成
# ---------------------------------------------------------------
def test_correction_signal():
    assert L.has_correction_signal("你错了,应该用 pytest")
    assert L.has_correction_signal("不对,重来")
    assert not L.has_correction_signal("帮我写个测试")
    assert not L.has_correction_signal("今天天气不错")


def test_tool_error_markers():
    assert L.is_tool_error("[ToolDispatch] terminal 参数错误: x")
    assert L.is_tool_error("[错误] 文件不存在")
    assert L.is_tool_error("xxx\n[EXIT CODE: 2]")
    assert not L.is_tool_error("正常输出")


# ============================================================
# 判据修正回归(2026-09-16):旧实现两个方向都错
# ============================================================
def test_successful_terminal_is_not_error():
    """**成功的 terminal 不能被判成失败。**

    回归:terminal 每条结果都附 "[EXIT CODE: N]"(成功是 0),而旧标记表里
    是 "[EXIT CODE: " 字面量 → 成功的命令被判成失败。后果:
      - 每次成功命令触发一次旁路 LLM "反思"(白烧 token)
      - 计入护栏 exact_failure(同命令同输出 6 次会被硬拦)
    """
    ok_ls = "[SHELL: git-bash]\napp.py  core.py\n\n[EXIT CODE: 0]"
    ok_pytest = "[SHELL: git-bash]\n186 passed in 13.2s\n\n[EXIT CODE: 0]"
    assert not L.is_tool_error(ok_ls)
    assert not L.is_tool_error(ok_pytest)


def test_exit_code_parsed_by_number():
    """退出码要看**数字**,不是看有没有这个标记。"""
    assert not L.is_tool_error("[EXIT CODE: 0]")
    assert L.is_tool_error("[EXIT CODE: 1]")
    assert L.is_tool_error("[EXIT CODE: 127]")
    # 多行/多处标记:任一个非 0 即失败
    assert L.is_tool_error("[EXIT CODE: 0]\n[EXIT CODE: 2]")
    # 非数字形态(理论上的异常输出)不误判
    assert not L.is_tool_error("[EXIT CODE: unknown]")


def test_not_executed_counts_as_error():
    """用户取消/拒绝 = "没有产出结果" → 必须算非成功。

    回归:这些文案不含任何旧标记,被判成成功 → "取消的 pytest"能当通过证据。
    """
    assert L.is_tool_error("[用户取消] 命令未执行")
    assert L.is_tool_error("[用户取消] 文件未读取")
    assert L.is_tool_error("[用户拒绝] 追加已取消")
    assert L.is_tool_error("[用户拒绝] 行修改已取消")
    assert L.is_tool_error(
        "[用户拒绝将内容写到项目目录之外] 写入已取消: /tmp/x")


def test_literal_error_word_in_content_still_matches():
    """已知局限:内容里恰好含 "[错误]" 字面量会误判(工具文案固定,风险可控)。

    这条**记录现状**而非认可:它是字符串匹配方案的固有代价,彻底解决要靠
    "工具结果结构化"(roadmap 待调研)。若将来结构化落地,此断言应删除。
    """
    assert L.is_tool_error("grep 结果: 日志里写着 [错误] 两个字")


def test_generate_lesson_valid_json():
    llm = FakeLLM([
        type("R", (), {"get": lambda self, k, d=None: json.dumps(
            [{"lesson": "做X会错,应该Y", "category": "测试"}])})(),
    ])
    items = L.generate_lesson(llm, "事件")
    assert items and items[0]["lesson"] == "做X会错,应该Y"


def test_generate_lesson_fenced():
    llm = FakeLLM([
        type("R", (), {"get": lambda self, k, d=None:
             "```json\n[{\"lesson\": \"规则B\", \"category\": \"终端\"}]\n```"})(),
    ])
    items = L.generate_lesson(llm, "事件")
    assert items and items[0]["lesson"] == "规则B"


def test_generate_lesson_invalid_returns_empty():
    llm = FakeLLM([type("R", (), {"get": lambda self, k, d=None: "不是JSON"})(),
                   type("R", (), {"get": lambda self, k, d=None: ""})()])
    assert L.generate_lesson(llm, "事件") == []
    assert L.generate_lesson(llm, "事件") == []


def test_generate_lesson_exception_returns_empty():
    class Boom:
        def chat(self, **kwargs):
            raise RuntimeError("网络错误")
    assert L.generate_lesson(Boom(), "事件") == []


# ---------------------------------------------------------------
# core._inject_lessons
# ---------------------------------------------------------------
def test_inject_lessons(tmp_path, store, sid):
    store.add_lesson(sid, "Windows 用 git-bash 别用 cmd", "terminal")
    store.add_lesson(sid, "写文件前先确认路径", "write_file")
    store.rebuild_search_index()
    agent = DummyAgent(None, create_default_registry(), system_prompt="P")
    agent.session_store = store
    agent.history = [{"role": "system", "content": "P"}]
    agent._inject_lessons("为什么我的命令报错")
    content = agent.history[0]["content"]
    assert "已知教训(规则):" in content
    assert "git-bash" in content
    # 幂等
    agent._inject_lessons("为什么我的命令报错")
    assert agent.history[0]["content"].count("已知教训(规则):") == 1


def test_inject_lessons_empty(tmp_path, store):
    agent = DummyAgent(None, create_default_registry(), system_prompt="P")
    agent.session_store = store
    agent.history = [{"role": "system", "content": "P"}]
    agent._inject_lessons("任意")
    assert agent.history[0]["content"] == "P"


def test_inject_lessons_pending_marked(tmp_path, store, sid):
    store.add_lesson(sid, "新教训待验证", "general")
    store.rebuild_search_index()
    agent = DummyAgent(None, create_default_registry(), system_prompt="P")
    agent.session_store = store
    agent.history = [{"role": "system", "content": "P"}]
    agent._inject_lessons("新教训")
    assert "待验证" in agent.history[0]["content"]


# ---------------------------------------------------------------
# 教训生成触发
# ---------------------------------------------------------------
def test_learn_from_correction(tmp_path, store, sid):
    llm = FakeLLM([
        type("R", (), {"get": lambda self, k, d=None: json.dumps(
            [{"lesson": "应该用 pytest", "category": "测试"}])})(),
    ])
    agent = DummyAgent(llm, create_default_registry(), system_prompt="P")
    agent.session_store = store
    agent.current_session_id = sid
    agent._learn_from_correction("你错了,应该用 pytest")
    assert len(store.list_lessons()) == 1
    assert store.list_lessons()[0]["lesson"] == "应该用 pytest"
    # 无纠正信号不触发
    agent._learn_from_correction("帮我写代码")
    assert len(store.list_lessons()) == 1


def test_learn_from_tool_error_limit(tmp_path, store, sid):
    llm = FakeLLM([
        type("R", (), {"get": lambda self, k, d=None: json.dumps(
            [{"lesson": "命令错了", "category": "terminal"}])})(),
    ])
    agent = DummyAgent(llm, create_default_registry(), system_prompt="P")
    agent.session_store = store
    agent.current_session_id = sid
    agent._tool_error_reflections = 0
    agent._learn_from_tool_error("terminal", "[错误] 语法错误")
    agent._learn_from_tool_error("terminal", "[错误] 又错了")
    assert len(store.list_lessons()) == 1  # 每轮限 1 次


# ---------------------------------------------------------------
# /lessons 命令
# ---------------------------------------------------------------
def test_lessons_command(store, sid):
    store.add_lesson(sid, "规则一", "general")
    lines = mm.handle_lessons_command("/lessons", store)
    assert "🧠 教训 (1 条)" in lines[0]
    assert "规则一" in lines[0] or any("规则一" in l for l in lines)
    lid = store.list_lessons()[0]["id"]
    lines = mm.handle_lessons_command(f"/lessons confirm {lid}", store)
    assert "已确认" in lines[0]
    lines = mm.handle_lessons_command(f"/lessons del {lid}", store)
    assert "已删除" in lines[0]
    assert store.list_lessons() == []
    lines = mm.handle_lessons_command("/lessons confirm abc", store)
    assert "用法" in lines[0]

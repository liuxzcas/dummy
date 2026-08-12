"""T1 持久化测试集(Phase 2 综合测试,见 docs/phase2-test-suite.md)。

覆盖 Phase 2.1 持久化行为:upsert 幂等 / 尾部删除 / 完整恢复 /
会话链 / UTC 时间 / 并发写 / 压缩重写一致性 / 归档表 / 索引一致。
全部 mock,零 API 成本,确定性断言。
"""

import os
import threading

from session_store import SessionStore


def make_store(tmp_path) -> SessionStore:
    return SessionStore(os.path.join(tmp_path, "persist.db"))


def sample_history() -> list[dict]:
    """典型历史:system + user + assistant(tool_calls) + tool 结果。"""
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "帮我看看文件"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "文件内容"},
        {"role": "assistant", "content": "看完了"},
    ]


def test_save_history_upsert_idempotent(tmp_path):
    """同 (session, sequence) 覆盖而非追加:重复保存不膨胀。"""
    store = make_store(tmp_path)
    sid = store.create_session()
    hist = sample_history()
    store.save_history(sid, hist)
    store.save_history(sid, hist)  # 第二次保存(模拟压缩后重写同序)
    loaded = store.load_history(sid)
    assert len(loaded) == len(hist)


def test_save_history_tail_delete(tmp_path):
    """保存更短的历史后,旧尾部消息被清(压缩重写语义)。"""
    store = make_store(tmp_path)
    sid = store.create_session()
    store.save_history(sid, sample_history())
    shorter = [{"role": "system", "content": "sys"},
               {"role": "user", "content": "新问题"}]
    store.save_history(sid, shorter)
    loaded = store.load_history(sid)
    assert len(loaded) == 2
    assert loaded[1]["content"] == "新问题"


def test_load_history_full_restore(tmp_path):
    """恢复完整性:顺序/角色/tool_call_id/content 全量保真。"""
    store = make_store(tmp_path)
    sid = store.create_session()
    store.save_history(sid, sample_history())
    loaded = store.load_history(sid)
    assert [m["role"] for m in loaded] == ["system", "user", "assistant", "tool", "assistant"]
    assert loaded[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert loaded[3]["tool_call_id"] == "call_1"
    assert loaded[4]["content"] == "看完了"


def test_create_session_and_latest(tmp_path):
    """会话链:create 返回 id,get_latest 拿到最新。"""
    store = make_store(tmp_path)
    s1 = store.create_session()
    s2 = store.create_session()
    assert s1 != s2
    assert store.get_latest_session_id() == s2


def test_time_stored_utc(tmp_path):
    """时间 UTC 存储(本地 UTC+8 显示时才转换,存储层不带本地偏移)。"""
    store = make_store(tmp_path)
    sid = store.create_session()
    store.save_history(sid, sample_history())
    conn = __import__("sqlite3").connect(os.path.join(tmp_path, "persist.db"))
    created = conn.execute(
        "SELECT created_at FROM sessions WHERE id = ?", (sid,)
    ).fetchone()[0]
    conn.close()
    assert "+00:00" in created or created.endswith("Z")


def test_concurrent_write_no_loss(tmp_path):
    """并发写:busy_timeout 保护下多线程写不同序列不丢消息。"""
    store = make_store(tmp_path)
    sid = store.create_session()
    errors: list = []

    def writer(seq: int):
        try:
            store.save_history(sid, [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": f"msg-{seq}"},
            ])
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    loaded = store.load_history(sid)
    assert len(loaded) == 2  # 各线程最后一条覆盖,总条数稳定


def test_compressed_rewrite_consistency(tmp_path):
    """压缩重写后恢复的是折叠后内容(头+标记+尾),不是原文。"""
    store = make_store(tmp_path)
    sid = store.create_session()
    folded = "头...[ToolResult 已截断: 原文 5000 字符]...尾"
    store.save_history(sid, [
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "c1", "content": folded},
    ])
    loaded = store.load_history(sid)
    assert loaded[1]["content"] == folded
    assert "已截断" in loaded[1]["content"]


def test_archive_idempotent_unique(tmp_path):
    """归档表 UNIQUE(session, tool_call_id):重复归档覆盖不新增。"""
    store = make_store(tmp_path)
    sid = store.create_session()
    item = {"tool_call_id": "c1", "content": "原文" * 100}
    store.archive_tool_results(sid, [item])
    store.archive_tool_results(sid, [item])
    conn = __import__("sqlite3").connect(os.path.join(tmp_path, "persist.db"))
    n = conn.execute("SELECT count(*) FROM tool_result_archive").fetchone()[0]
    conn.close()
    assert n == 1


def test_archive_retrieve_original(tmp_path):
    """决策 C:归档表取回完整原文。"""
    store = make_store(tmp_path)
    sid = store.create_session()
    original = "X" * 300 + "port 8080" + "Y" * 300
    store.archive_tool_results(sid, [{"tool_call_id": "c9", "content": original}])
    assert store.get_archived_tool_result(sid, "c9") == original
    assert store.get_archived_tool_result(sid, "nope") is None


def test_index_rebuild_matches_db(tmp_path):
    """索引重建与库一致:新消息 + 归档原文都能搜到。"""
    store = make_store(tmp_path)
    sid = store.create_session()
    store.save_history(sid, [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "上下文压缩模块设计"},
    ])
    store.archive_tool_results(sid, [{"tool_call_id": "t1", "content": "ERROR: port 8080"}])
    store.rebuild_search_index()
    assert any(h["source"] == "message" for h in store.search("上下文"))
    assert any(h["source"] == "archive" for h in store.search("port 8080"))


def test_list_sessions_counts(tmp_path):
    """会话列表含消息计数。"""
    store = make_store(tmp_path)
    sid = store.create_session()
    store.save_history(sid, sample_history())
    sessions = store.list_sessions()
    assert sessions[0]["id"] == sid
    assert sessions[0]["message_count"] == len(sample_history())

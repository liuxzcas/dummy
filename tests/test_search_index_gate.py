"""
tests/test_search_index_gate.py — FTS 索引的"脏才重建"门控

背景(2026-11-23):
原实现里 search() 每次都无条件 rebuild_search_index()。库长到 4800+ 行后
单次重建 ~22 秒,而 core 每轮注入记忆 + 教训要调 2 次 search → 每轮白烧 44 秒。
实测两条用例因此各慢 45 秒(浪费 62% 的全量测试时长)。

本文件锁住三件事:
1. 行数没变时 search() **不重建**(这是提速的来源)
2. 行数变了必定重建(索引不能静默过期)
3. UPDATE 改了内容但行数不变时,mark_index_dirty() 能强制重建
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from session_store import SessionStore  # noqa: E402


pytestmark = pytest.mark.core   # 测试分组见 pytest.ini


@pytest.fixture()
def store(tmp_path):
    return SessionStore(db_path=str(tmp_path / "t.db"))


def _add_message(store, sid, seq, content, role="user"):
    """直接写 messages 表 —— 走 store 的公开方法会同时写索引,测不到门控。"""
    import json
    import sqlite3
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute(
            "INSERT INTO messages (session_id, sequence, role, content, payload, created_at) "
            "VALUES (?,?,?,?,?, datetime('now'))",
            (sid, seq, role, content, json.dumps({"role": role, "content": content})),
        )
        conn.commit()
    finally:
        conn.close()


def test_first_search_builds_once(store):
    """首次搜索必须建一次索引(否则搜不到东西)。"""
    _add_message(store, "s1", 1, "压缩策略讨论")
    assert store.ensure_search_index() is True


def test_second_search_does_not_rebuild(store):
    """行数没变 → 不重建。这是提速的关键断言。"""
    _add_message(store, "s1", 1, "压缩策略讨论")
    store.ensure_search_index()                 # 首次建
    assert store.ensure_search_index() is False  # 第二次必须跳过


def test_new_row_triggers_rebuild(store):
    """行数变化 → 必须重建,索引不能静默过期。"""
    _add_message(store, "s1", 1, "压缩策略讨论")
    store.ensure_search_index()
    _add_message(store, "s1", 2, "新增一条消息")
    assert store.ensure_search_index() is True


def test_new_message_is_searchable_after_rebuild(store):
    """端到端:新增内容能搜到。"""
    _add_message(store, "s1", 1, "压缩策略讨论")
    store.search("压缩")
    _add_message(store, "s1", 2, "索引重建后应该能搜到绿萝")
    hits = store.search("绿萝")
    assert any("绿萝" in h["snippet"] for h in hits), hits


def test_mark_dirty_forces_rebuild(store):
    """行数不变但内容变了(UPDATE 路径) → 靠显式标脏。"""
    _add_message(store, "s1", 1, "旧的事实内容")
    store.ensure_search_index()
    assert store.ensure_search_index() is False
    store.mark_index_dirty()
    assert store.ensure_search_index() is True


def test_snapshot_is_recorded(store):
    """重建后必须留下快照,否则下次永远重建。"""
    import sqlite3
    _add_message(store, "s1", 1, "abc")
    store.ensure_search_index()
    conn = sqlite3.connect(store.db_path)
    try:
        v = conn.execute("SELECT v FROM meta WHERE k='fts_snapshot'").fetchone()
    finally:
        conn.close()
    assert v and v[0] == "1,0,0,0", v      # messages=1, 其余 0


def test_index_counts_match_sources(store):
    """索引行数必须与源表一致 —— executemany 改造后语义不能变。"""
    import sqlite3
    for i in range(3):
        _add_message(store, "s1", i + 1, f"内容{i}")
    store.ensure_search_index()
    conn = sqlite3.connect(store.db_path)
    try:
        for table in ("fts_en", "fts_zh"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE source='message'"
            ).fetchone()[0]
            assert n == 3, (table, n)
    finally:
        conn.close()

"""T3 全文搜索测试集(Phase 2 综合测试,见 docs/phase2-test-suite.md)。

覆盖 Phase 2.3b 检索行为:英文词级/词干/前缀、中文 trigram/2 字退化/
混合片段、归档找回、source 过滤、system 排除、排序、边界、与压缩联动。
从 2.3b ad-hoc 验证固化;全部 mock,零 API 成本。
"""

import os
import sqlite3

from session_store import SessionStore


def make_store(tmp_path) -> SessionStore:
    return SessionStore(os.path.join(tmp_path, "fts.db"))


def seed(store) -> tuple[str, str]:
    """造标准数据:中文 + 英文 + 折叠后 tool + 归档原文。返回 (sid_a, sid_b)。"""
    sid_a = store.create_session()
    sid_b = store.create_session()
    store.save_history(sid_a, [
        {"role": "system", "content": "sys template systemsecretexclusionkeyword"},
        {"role": "user", "content": "上下文压缩模块的设计文档在 docs/context-compression.md"},
        {"role": "assistant", "content": "compressing the context with L1 folding"},
        {"role": "user", "content": "折叠后的 tool 结果只保留头尾"},
        {"role": "tool", "tool_call_id": "t1",
         "content": "头...[ToolResult 已截断: 原文 5000 字符]...尾 ERROR: check log"},
    ])
    store.save_history(sid_b, [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Transformer attention research notes"},
        {"role": "assistant", "content": "search the archive for details"},
    ])
    store.archive_tool_results(sid_a, [
        {"tool_call_id": "t1",
         "content": "L" * 100 + "ERROR: port 8080 出现在中间段" + "D" * 100},
    ])
    return sid_a, sid_b


def clean(s) -> str:
    return (s or "").replace("[", "").replace("]", "")


def test_english_word_level_precision(tmp_path):
    """'search' 命中自己的文本,不命中 'research'(词级,防子串污染)。"""
    store = make_store(tmp_path)
    seed(store)
    hits = store.search("search")
    assert any("search the archive" in clean(h["snippet"]) for h in hits)
    assert not any("research" in clean(h["snippet"]) for h in hits)


def test_porter_stemming(tmp_path):
    """'compression' 命中 'compressing'(词干化)。"""
    store = make_store(tmp_path)
    seed(store)
    hits = store.search("compression")
    assert any("compressing" in clean(h["snippet"]) for h in hits)


def test_prefix_query(tmp_path):
    """'transfor*' 命中 Transformer(前缀不被短语化)。"""
    store = make_store(tmp_path)
    seed(store)
    hits = store.search("transfor*")
    assert any("Transformer" in clean(h["snippet"]) for h in hits)


def test_chinese_trigram(tmp_path):
    """'上下文' 命中中文消息(trigram 子串)。"""
    store = make_store(tmp_path)
    seed(store)
    hits = store.search("上下文")
    assert any("上下文压缩模块" in clean(h["snippet"]) for h in hits)


def test_chinese_two_char_like_fallback(tmp_path):
    """2 字中文 '压缩' 走 LIKE 退化命中。"""
    store = make_store(tmp_path)
    seed(store)
    hits = store.search("压缩")
    assert any("上下文压缩模块" in clean(h["snippet"]) for h in hits)


def test_mixed_query_segments(tmp_path):
    """'L1 压缩' 片段拆分:中英文都命中。"""
    store = make_store(tmp_path)
    seed(store)
    hits = store.search("L1 压缩")
    assert any("上下文压缩模块" in clean(h["snippet"]) for h in hits)
    assert any("L1 folding" in clean(h["snippet"]) for h in hits)


def test_archive_original_retrievable(tmp_path):
    """决策 C:折叠原文(中间信息)只能从归档表搜到。"""
    store = make_store(tmp_path)
    seed(store)
    hits = store.search("port 8080")
    assert len(hits) > 0
    assert hits[0]["source"] == "archive"
    assert store.search("port 8080", source="message") == []


def test_source_filter_match_path(tmp_path):
    """MATCH 路径 source 过滤生效。"""
    store = make_store(tmp_path)
    seed(store)
    assert store.search("pytest", source="message") == []
    hits = store.search("上下文", source="archive")
    assert all(h["source"] == "archive" for h in hits) and hits == []


def test_source_filter_like_path(tmp_path):
    """LIKE 退化路径 source 过滤生效(漏透传是历史 bug)。"""
    store = make_store(tmp_path)
    seed(store)
    hits = store.search("压缩", source="archive")
    assert all(h["source"] == "archive" for h in hits) and hits == []


def test_system_template_not_indexed(tmp_path):
    """system 模板内容不索引(结果不被模板污染)。"""
    store = make_store(tmp_path)
    seed(store)
    assert store.search("systemsecretexclusionkeyword") == []


def test_bm25_ordering(tmp_path):
    """bm25 升序(越小越相关)。"""
    store = make_store(tmp_path)
    seed(store)
    scores = [h["score"] for h in store.search("压缩")]
    assert scores == sorted(scores)


def test_memory_source_searchable(tmp_path):
    """记忆条目作为第三索引源可检索(2.4 依赖)。"""
    store = make_store(tmp_path)
    seed(store)
    store.add_memory("s1", "项目使用 pytest 测试", "项目", 0.8)
    hits = store.search("pytest", source="memory")
    assert any(h["source"] == "memory" for h in hits)


def test_memory_two_char_like(tmp_path):
    """2 字中文记忆经 LIKE 分支命中(含 memory 源)。"""
    store = make_store(tmp_path)
    seed(store)
    store.add_memory("s1", "用户偏好中文交流", "偏好", 0.9)
    hits = store.search("偏好", source="memory")
    assert len(hits) >= 1 and hits[0]["source"] == "memory"


def test_edge_cases(tmp_path):
    """空查询 / 特殊字符 / 重建幂等 / 表存在。"""
    store = make_store(tmp_path)
    seed(store)
    assert store.search("") == []
    assert isinstance(store.search('he said "hi" AND'), list)
    store.rebuild_search_index()
    assert len(store.search("压缩")) > 0
    conn = sqlite3.connect(os.path.join(tmp_path, "fts.db"))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"fts_en", "fts_zh"} <= tables


def test_index_after_compressed_rewrite(tmp_path):
    """压缩重写后索引覆盖折叠后内容(与压缩联动)。"""
    store = make_store(tmp_path)
    sid = store.create_session()
    store.save_history(sid, [
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "c1", "content": "原文很长内容在这里"},
    ])
    # 模拟压缩重写:同一 sequence 覆盖为折叠后内容
    store.save_history(sid, [
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "c1",
         "content": "头...[ToolResult 已截断]...尾"},
    ])
    hits = store.search("已截断")
    assert len(hits) == 1
    # 原文信息已不在索引中(被折叠替换)
    assert store.search("原文很长") == []

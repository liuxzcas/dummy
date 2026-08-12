"""T4 跨 Session 记忆测试集(Phase 2 综合测试,见 docs/phase2-test-suite.md)。

覆盖 Phase 2.4 记忆行为:CRUD / 冲突覆盖 / FTS 双路径检索 / 解析三级容错 /
抽取写入与降级 / 事件日志 / 注入组装与防累积 / 字符上限 / 旁路不污染 /
检索词提炼(方案 A,PlugMem 意图路由)/ CLI。

注意:chat() 的 LLM 调用序列 = expand_query(1) + 主循环(1+) + 抽取(1),
SeqLLM 序列按此排布。
"""

import io
import os
from contextlib import redirect_stdout

from core import DummyAgent
from memory import MemoryExtractor, parse_facts_response, parse_terms
from session_store import SessionStore
from tools import ToolRegistry


def make_store(tmp_path, name="memory.db") -> SessionStore:
    return SessionStore(os.path.join(tmp_path, name))


class FakeMsg:
    def __init__(self, content="", tool_calls=None, reasoning=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning
        self.model_extra = {}

    def to_dict(self):
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        return d


class SeqLLM:
    """顺序返回预设消息;可注入异常。"""

    def __init__(self, seq):
        self.seq = list(seq)
        self.last_reasoning = None
        self.last_usage = None

    def chat(self, messages, **kwargs):
        self.sent_messages = messages
        item = self.seq.pop(0)
        if isinstance(item, Exception):
            raise item
        self.last_reasoning = item.reasoning_content
        return item


class EmptyRegistry(ToolRegistry):
    def list_tools(self):
        return []


def make_agent(llm_seq, store) -> DummyAgent:
    agent = DummyAgent(SeqLLM(llm_seq), EmptyRegistry(), system_prompt="sys")
    agent.session_store = store
    agent.memory_extractor.store = store  # 同步替换,防污染真实库
    agent.current_session_id = store.create_session()
    return agent


def chat_and_capture(agent, user_input):
    buf = io.StringIO()
    with redirect_stdout(buf):
        out = agent.chat(user_input)
    return out, buf.getvalue()


# ---- 表层:CRUD / 冲突 / hits ----

def test_memory_crud(tmp_path):
    store = make_store(tmp_path)
    m1 = store.add_memory("s1", "用户偏好中文", "偏好", 0.9)
    m2 = store.add_memory("s1", "测试用 pytest", "项目", 0.8)
    assert m1 == 1 and m2 == 2
    assert [m["id"] for m in store.list_memories()] == [2, 1]
    assert store.delete_memory(m2) is True
    assert store.delete_memory(999) is False
    assert len(store.list_memories()) == 1


def test_replace_id_overwrite_commits(tmp_path):
    """冲突覆盖:replace_id 更新同 id(commit 生效是历史 bug)。"""
    store = make_store(tmp_path)
    m1 = store.add_memory("s1", "旧事实", "偏好", 0.7)
    store.add_memory("s2", "新事实", "偏好", 0.95, replace_id=m1)
    mems = {m["id"]: m for m in store.list_memories()}
    assert len(mems) == 1
    assert mems[m1]["fact"] == "新事实"
    assert mems[m1]["confidence"] == 0.95
    assert mems[m1]["hits"] == 0  # 覆盖保留使用统计


def test_hits_increment(tmp_path):
    store = make_store(tmp_path)
    m1 = store.add_memory("s1", "事实A", "偏好")
    store.increment_memory_hits([m1])
    assert {m["id"]: m["hits"] for m in store.list_memories()}[m1] == 1


# ---- 检索层:FTS memory 源 ----

def test_memory_searchable_match_and_like(tmp_path):
    store = make_store(tmp_path)
    store.add_memory("s1", "pytest is the test framework", "项目", 0.8)
    store.add_memory("s1", "用户偏好中文交流", "偏好", 0.9)
    assert any(h["source"] == "memory" for h in store.search("pytest", source="memory"))
    hits = store.search("偏好", source="memory")  # 2 字 → LIKE 分支
    assert len(hits) >= 1 and hits[0]["source"] == "memory"
    assert store.search("pytest", source="message") == []


# ---- 解析层:三级容错 ----

def test_parse_facts_json_fence_regex(tmp_path):
    assert parse_facts_response('[{"fact": "a", "category": "偏好", "confidence": 0.9, "replace_id": null}]')[0]["fact"] == "a"
    assert parse_facts_response('```json\n[{"fact": "b"}]\n```')[0]["fact"] == "b"
    assert parse_facts_response('前言 [{"fact": "c"}] 后记')[0]["fact"] == "c"


def test_parse_facts_invalid_dropped(tmp_path):
    assert parse_facts_response("完全不是 JSON") == []
    assert parse_facts_response('[{"x": 1}]') == []


# ---- 检索词提炼(方案 A,PlugMem 意图路由)----

def test_parse_terms_fallback(tmp_path):
    assert parse_terms('["前端框架", "后端框架"]') == ["前端框架", "后端框架"]
    assert parse_terms('```json\n["a"]\n```') == ["a"]
    assert parse_terms('回答: "技术栈" 和 "部署"') == ["技术栈", "部署"]
    assert parse_terms("不是 JSON") == []


def test_expand_query_normal_and_fallback(tmp_path):
    store = make_store(tmp_path)
    log = os.path.join(tmp_path, "m.jsonl")
    ext = MemoryExtractor(SeqLLM([FakeMsg('["前端框架", "技术选型"]')]), store, log)
    assert ext.expand_query("前后端用什么技术栈?") == ["前端框架", "技术选型"]
    ext2 = MemoryExtractor(SeqLLM([RuntimeError("boom")]), store, log)
    assert ext2.expand_query("任意问句") == ["任意问句"]  # 异常回退原句


def test_inject_uses_expanded_terms(tmp_path):
    """核心:问句与记忆无共享关键词,靠提炼词命中(旧行为失败)。"""
    store = make_store(tmp_path)
    store.add_memory("s1", "前端框架用 Vue", "项目", 0.9)
    agent = make_agent([FakeMsg('["前端框架"]'), FakeMsg("好的")], store)
    out, printed = chat_and_capture(agent, "前后端用什么技术栈?")
    assert "🧠 注入记忆" in printed and "Vue" in printed
    assert "Vue" in agent.history[0]["content"]
    assert out == "好的"


# ---- 抽取层:写入 / 冲突 / 降级 / 事件日志 ----

def test_extractor_write_and_conflict(tmp_path):
    store = make_store(tmp_path)
    log = os.path.join(tmp_path, "m.jsonl")
    ext = MemoryExtractor(
        SeqLLM([FakeMsg('[{"fact": "用户讨厌英语", "category": "偏好", "confidence": 0.7, "replace_id": null}]')]),
        store, log)
    n, c = ext.extract_from_history([{"role": "user", "content": "我讨厌英语"}], "s9")
    assert n == 1 and c == 0
    rid = [m["id"] for m in store.list_memories() if "讨厌英语" in m["fact"]][0]
    ext2 = MemoryExtractor(
        SeqLLM([FakeMsg(f'[{{"fact": "用户强烈讨厌英语", "category": "偏好", "confidence": 0.9, "replace_id": {rid}}}]')]),
        store, log)
    n2, c2 = ext2.extract_from_history([{"role": "user", "content": "英语真烦"}], "s9")
    mems = {m["id"]: m for m in store.list_memories()}
    assert c2 == 1 and mems[rid]["fact"] == "用户强烈讨厌英语"


def test_extractor_exception_degrades(tmp_path):
    store = make_store(tmp_path)
    log = os.path.join(tmp_path, "m.jsonl")
    ext = MemoryExtractor(SeqLLM([RuntimeError("boom")]), store, log)
    n, c = ext.extract_from_history([{"role": "user", "content": "hi"}], "s9")
    assert n == 0 and c == 0
    assert len(store.list_memories()) == 0  # 不落任何脏数据


def test_extractor_event_log(tmp_path):
    store = make_store(tmp_path)
    log = os.path.join(tmp_path, "m.jsonl")
    ext = MemoryExtractor(SeqLLM([FakeMsg('[{"fact": "x", "category": "其他", "confidence": 0.5, "replace_id": null}]')]), store, log)
    ext.extract_from_history([{"role": "user", "content": "hi"}], "s9")
    ext2 = MemoryExtractor(SeqLLM([RuntimeError("boom")]), store, log)
    ext2.extract_from_history([{"role": "user", "content": "hi"}], "s9")
    with open(log, encoding="utf-8") as f:
        content = f.read()
    assert "extract_failed" in content and "extract" in content


# ---- 注入层:组装 / 防累积 / 上限 / hits ----

def test_inject_memory_block_and_print(tmp_path):
    store = make_store(tmp_path)
    store.add_memory("s1", "项目使用 pytest 测试", "项目", 0.8)
    agent = make_agent([FakeMsg('["pytest"]'), FakeMsg("好的")], store)
    out, printed = chat_and_capture(agent, "pytest 相关的问题")
    assert "🧠 注入记忆" in printed and "pytest" in printed
    assert "已知事实(记忆):" in agent.history[0]["content"]
    assert out == "好的"


def test_inject_hits_only_matched(tmp_path):
    store = make_store(tmp_path)
    store.add_memory("s1", "用户偏好中文", "偏好", 0.9)
    store.add_memory("s1", "项目使用 pytest", "项目", 0.8)
    agent = make_agent([FakeMsg('["pytest"]'), FakeMsg("好的")], store)
    chat_and_capture(agent, "pytest 的问题")
    hits = {m["id"]: m["hits"] for m in store.list_memories()}
    assert hits == {1: 0, 2: 1}  # 只递增命中的条目


def test_inject_no_accumulation_across_chats(tmp_path):
    store = make_store(tmp_path)
    store.add_memory("s1", "项目使用 pytest", "项目", 0.8)
    agent = make_agent([FakeMsg('["pytest"]'), FakeMsg("好的")], store)
    chat_and_capture(agent, "pytest 问题一")
    agent.llm.seq = [FakeMsg('["pytest"]'), FakeMsg("好的2"),
                     FakeMsg('[{"fact": "二次抽取", "category": "其他", "confidence": 0.5, "replace_id": null}]')]
    chat_and_capture(agent, "pytest 问题二")
    assert agent.history[0]["content"].count("已知事实(记忆):") == 1


def test_inject_char_cap(tmp_path):
    store = make_store(tmp_path)
    store.add_memory("s1", "很长的记忆" * 100, "偏好", 0.8)  # 500 字符
    agent = make_agent([FakeMsg('["记忆"]'), FakeMsg("好的")], store)
    chat_and_capture(agent, "记忆相关")
    block = agent.history[0]["content"]
    idx = block.find("已知事实(记忆):")
    if idx >= 0:
        assert len(block[idx:]) <= 420  # 段总长 ≤ 400 + 标题余量


# ---- 旁路与 CLI ----

def test_extract_bypass_no_pollution(tmp_path):
    store = make_store(tmp_path)
    agent = make_agent([FakeMsg('["记住"]'), FakeMsg("这是回答")], store)
    agent.llm.seq = [FakeMsg('["记住"]'), FakeMsg("这是回答"),
                     FakeMsg('[{"fact": "用户说记住了", "category": "其他", "confidence": 0.5, "replace_id": null}]')]
    out, printed = chat_and_capture(agent, "记住这句话")
    assert "已抽取 1 条事实" in printed
    assert all("fact" not in str(m.get("content", "")) for m in agent.history)
    assert len(store.list_memories()) == 1
    assert out == "这是回答"


def test_memories_cli(tmp_path):
    import main as mm
    store = make_store(tmp_path)
    store.add_memory("s1", "用户偏好中文", "偏好", 0.9)
    lines = mm.handle_memories_command("/memories", store)
    assert any("用户偏好中文" in l for l in lines)
    mid = store.list_memories()[0]["id"]
    assert mm.handle_memories_command(f"/memories del {mid}", store)[0].startswith("已删除记忆")
    assert mm.handle_memories_command("/memories", store)[0].startswith("🧠 (暂无")
    assert mm.handle_memories_command("/memories del abc", store)[0].startswith("用法")
    assert "不存在" in mm.handle_memories_command("/memories del 999", store)[0]

"""记忆测试集(分支 plugmem-v2,PlugMem 论文实现)。

覆盖:知识单元 CRUD / 概念路由检索 / 结构化抽取(含更新演化)/
概念生成 / 推理蒸馏 / core 注入集成(防累积/上限/旁路)/ CLI。
全部 mock,零 API 成本。
"""

import io
import os
from contextlib import redirect_stdout

from core import DummyAgent
from plugmem import PlugMemMemory, parse_json_array
from session_store import SessionStore
from tools import ToolRegistry


def make_store(tmp_path, name="pm.db") -> SessionStore:
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
    agent.memory.store = store  # 同步替换,防污染真实库
    agent.current_session_id = store.create_session()
    return agent


def chat_and_capture(agent, user_input):
    buf = io.StringIO()
    with redirect_stdout(buf):
        out = agent.chat(user_input)
    return out, buf.getvalue()


# ---- 单元层:CRUD / 概念 ----

def test_unit_crud_and_concepts(tmp_path):
    store = make_store(tmp_path)
    u1 = store.add_unit("s1", "semantic", "用户偏好中文", ["语言", "偏好"])
    u2 = store.add_unit("s1", "procedural", "发布流程:先跑测试", ["发布", "流程"])
    assert u1 == 1 and u2 == 2
    units = {u["id"]: u for u in store.list_units()}
    assert units[1]["type"] == "semantic" and units[1]["concepts"] == ["语言", "偏好"]
    assert units[2]["type"] == "procedural"
    # 更新:同 id 保留,概念替换
    store.add_unit("s2", "semantic", "用户偏好中文(更新)", ["语言", "偏好", "沟通"], update_unit_id=1)
    units = {u["id"]: u for u in store.list_units()}
    assert len(units) == 2
    assert units[1]["text"].endswith("(更新)")
    assert units[1]["concepts"] == ["语言", "偏好", "沟通"]
    assert store.delete_unit(2) is True
    assert store.delete_unit(999) is False


def test_search_units_by_concepts(tmp_path):
    store = make_store(tmp_path)
    store.add_unit("s1", "semantic", "前端框架用 Vue", ["前端框架", "技术栈"])
    store.add_unit("s1", "semantic", "后端用 FastAPI", ["后端框架", "技术栈"])
    store.add_unit("s1", "semantic", "用户偏好中文", ["语言", "偏好"])
    hits = store.search_units_by_concepts(["前端框架"])
    assert len(hits) == 1 and hits[0]["text"] == "前端框架用 Vue"
    hits = store.search_units_by_concepts(["技术栈"])
    assert len(hits) == 2  # 概念并集去重
    hits = store.search_units_by_concepts(["不存在概念"])
    assert hits == []


# ---- 解析层 ----

def test_parse_json_array(tmp_path):
    assert parse_json_array('["a", "b"]') == ["a", "b"]
    assert parse_json_array('```json\n["a"]\n```') == ["a"]
    assert parse_json_array('回答: "概念" 和 "标签"') == ["概念", "标签"]
    assert parse_json_array("不是 JSON") == []
    objs = parse_json_array('[{"type": "semantic", "text": "x", "concepts": ["c"]}]')
    assert isinstance(objs, list) and objs[0]["type"] == "semantic"


# ---- 结构化(3.1)----

def test_structure_from_history_write_and_update(tmp_path):
    store = make_store(tmp_path)
    log = os.path.join(tmp_path, "m.jsonl")
    structure_json = (
        '[{"type": "semantic", "text": "项目预算是 3000 元", '
        '"concepts": ["预算", "项目"], "update_unit_id": null}, '
        '{"type": "semantic", "text": "上线日期 8 月 20 日", '
        '"concepts": ["日期", "上线"], "update_unit_id": null}]'
    )
    pm = PlugMemMemory(SeqLLM([FakeMsg(structure_json)]), store, log)
    n, u = pm.structure_from_history([{"role": "user", "content": "预算 3000"}], "s9")
    assert n == 2 and u == 0
    units = store.list_units()
    assert len(units) == 2
    # 更新演化:同主题新证据覆盖
    target_id = units[1]["id"]
    update_json = (
        '[{"type": "semantic", "text": "项目预算是 8000 元", '
        '"concepts": ["预算", "项目"], "update_unit_id": ' + str(target_id) + '}]'
    )
    pm2 = PlugMemMemory(SeqLLM([FakeMsg(update_json)]), store, log)
    n2, u2 = pm2.structure_from_history([{"role": "user", "content": "改 8000"}], "s9")
    assert n2 == 1 and u2 == 1
    units = {u["id"]: u for u in store.list_units()}
    assert units[1]["text"] == "项目预算是 8000 元"  # 覆盖,不并存


def test_structure_exception_degrades(tmp_path):
    store = make_store(tmp_path)
    log = os.path.join(tmp_path, "m.jsonl")
    pm = PlugMemMemory(SeqLLM([RuntimeError("boom")]), store, log)
    n, u = pm.structure_from_history([{"role": "user", "content": "hi"}], "s9")
    assert n == 0 and u == 0
    assert store.list_units() == []
    pm2 = PlugMemMemory(SeqLLM([FakeMsg("无法解析")]), store, log)
    n2, _ = pm2.structure_from_history([{"role": "user", "content": "hi"}], "s9")
    assert n2 == 0
    with open(log, encoding="utf-8") as f:
        content = f.read()
    assert "extract_failed" in content


# ---- 检索(3.2)与推理(3.3)----

def test_retrieve_concepts_normal_and_fallback(tmp_path):
    store = make_store(tmp_path)
    log = os.path.join(tmp_path, "m.jsonl")
    pm = PlugMemMemory(SeqLLM([FakeMsg('["技术栈", "前端框架"]')]), store, log)
    assert pm.retrieve_concepts("前后端用什么?") == ["技术栈", "前端框架"]
    pm2 = PlugMemMemory(SeqLLM([RuntimeError("boom")]), store, log)
    assert pm2.retrieve_concepts("任意问句") == ["任意问句"]


def test_distill_normal_and_fallback(tmp_path):
    store = make_store(tmp_path)
    log = os.path.join(tmp_path, "m.jsonl")
    pm = PlugMemMemory(SeqLLM([FakeMsg("前端用 Vue,后端用 FastAPI")]), store, log)
    units = [{"type": "semantic", "text": "前端框架用 Vue"},
             {"type": "semantic", "text": "后端用 FastAPI"}]
    assert pm.distill("技术栈?", units) == "前端用 Vue,后端用 FastAPI"
    pm2 = PlugMemMemory(SeqLLM([RuntimeError("boom")]), store, log)
    out = pm2.distill("技术栈?", units)
    assert "前端框架用 Vue" in out  # 失败回退原文拼接


# ---- core 集成:注入(概念→单元→蒸馏)与结构化 ----

def test_inject_concept_route_distill(tmp_path):
    """核心:问句'技术栈?' 与单元无共享词,靠概念层路由命中并蒸馏。"""
    store = make_store(tmp_path)
    store.add_unit("s1", "semantic", "前端框架用 Vue", ["前端框架", "技术栈"])
    store.add_unit("s1", "semantic", "后端用 FastAPI", ["后端框架", "技术栈"])
    # chat 调用序列:retrieve_concepts(1) + distill(1) + 主循环(1) + structure(1)
    agent = make_agent(
        [FakeMsg('["技术栈", "前端框架"]'), FakeMsg("前端 Vue,后端 FastAPI"),
         FakeMsg("好的"), FakeMsg("[]")],
        store)
    out, printed = chat_and_capture(agent, "前后端分别用什么技术栈?")
    assert "🧠 注入记忆" in printed
    assert "已知事实(记忆):" in agent.history[0]["content"]
    assert "Vue" in agent.history[0]["content"] and "FastAPI" in agent.history[0]["content"]
    assert out == "好的"


def test_inject_no_accumulation(tmp_path):
    store = make_store(tmp_path)
    store.add_unit("s1", "semantic", "项目使用 pytest", ["测试工具"])
    agent = make_agent(
        [FakeMsg('["测试"]'), FakeMsg("用 pytest"), FakeMsg("好的"), FakeMsg("[]")],
        store)
    chat_and_capture(agent, "测试用什么?")
    agent.llm.seq = [FakeMsg('["测试"]'), FakeMsg("用 pytest"),
                     FakeMsg("好的2"), FakeMsg("[]")]
    chat_and_capture(agent, "再问测试")
    assert agent.history[0]["content"].count("已知事实(记忆):") == 1


def test_extract_bypass_no_pollution(tmp_path):
    store = make_store(tmp_path)
    agent = make_agent(
        [FakeMsg("[]"), FakeMsg("这是回答"),
         FakeMsg('[{"type": "semantic", "text": "用户说记住了", "concepts": ["记忆"], "update_unit_id": null}]')],
        store)
    out, printed = chat_and_capture(agent, "记住这句话")
    assert "已结构化 1 个知识单元" in printed
    assert all("用户说记住了" not in str(m.get("content", "")) for m in agent.history)
    assert len(store.list_units()) == 1
    assert out == "这是回答"


def test_memories_cli(tmp_path):
    import main as mm
    store = make_store(tmp_path)
    store.add_unit("s1", "semantic", "用户偏好中文", ["语言", "偏好"])
    lines = mm.handle_memories_command("/memories", store)
    assert any("用户偏好中文" in l for l in lines)
    assert any("concepts=" in l for l in lines)
    mid = store.list_units()[0]["id"]
    assert mm.handle_memories_command(f"/memories del {mid}", store)[0].startswith("已删除知识单元")
    assert mm.handle_memories_command("/memories", store)[0].startswith("🧠 (暂无")
    assert mm.handle_memories_command("/memories del abc", store)[0].startswith("用法")
    assert "不存在" in mm.handle_memories_command("/memories del 999", store)[0]

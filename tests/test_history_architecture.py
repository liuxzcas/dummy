"""历史层架构实验的统一判据(三路共用,不可各自改写)。

为什么必须统一:三路各自写测试,就会出现"谁写的测试松谁通过得多",
比较失去意义。所以判据固定为三类。

【重要】判据经历过两次校正,记录在此以免重蹈:
  校正 1:最初只有一条 AR1(直接 save→load)。它测的是"持久层忠实读取",
          完全绕开 chat(),所以对"收口类"方案(路线 3)无从判定。
  校正 2:AR-store 组原本被当作方案验收标准。但真实系统里历史的**唯一写入者
          是 chat()**,"外部直接 save 非法历史"是个假问题(持久层容忍度,
          不是系统是否会产生非法历史)。故降级为参考项,新增 AR-stop 组。

【SOFT 组】软标准 —— 通过加分,不通过不判失败

  SOFT1 磁盘上**从未出现过**非法状态(而非"事后修好")
     判法:给 SessionStore.save_history 装一个探针,记录每次落库时的历史快照;
     停机场景走完后,检查**每个快照**是否合法。
       - 路线 5(批次原子落库):每个快照都合法 → 通过(加分)
       - 路线 3(每条结果后落库,退出时修):中间快照有非法 → 不通过(但仍可采纳)

  为什么要这条:它把"及时修复"与"不让发生"分开。前者依赖一个隐含假设——
  进程一定能走到 finally。而 **kill -9 / 断电 / 任务管理器结束进程不会执行
  finally**,那时路线 3 的磁盘上就留着半个批次,路线 5 天然免疫。

  但它是**软**标准:两种方案在"停机后磁盘合法"这个用户可见结果上等价,
  所以不通过的路线只要不出问题,照样采纳。

【AR 组】不变量断言 —— 任何路线都必须满足

  A. AR-chat(经 chat() 的退出路径)
     这轮经过 chat(),无论怎么结束(正常返回 / 异常 / Ctrl+C / 轮次用尽),
     落到磁盘的历史都必须合法。
     → "收口类"方案(路线 3)的考点。

  B. AR-stop(批次中途停机的真实场景)★核心
     LLM 一批返回多个 tool_calls,执行到中途进程"死掉"(抛异常)。
     此时**磁盘上**的历史必须合法。
     → 这是区分"事后修"与"不让发生"的关键判据。
       路线 5(原子批次:批次不完整就不落库)在这里应当全绿;
       靠"每条工具结果后落库 + 事后 repair"的方案在这里会红。

  C. AR-store(持久层容忍度,参考项,不作为方案验收)
     非法历史直接写进库再读回。这是持久层健壮性问题,不是系统是否
     会产生非法历史。**任何路线都不因这一组判优劣。**

  AR4/AR5/AR7:压缩契约、save→load 无损、干净历史零修复动作。

【MET 组】结构指标(度量"不变量有没有归属者")
  MET1 兜底点调用数(现状 7)   MET2 主流程(chat)改动行数
  MET3 全量测试需改断言数       MET4 压缩前后的配对耦合

用法:三路 worktree 都**不许改**本文件。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
from conftest import _Msg, _TC, ScriptedLLM  # noqa: E402
from session_store import SessionStore  # noqa: E402
from tools import create_default_registry  # noqa: E402

TWO_CALLS = {"role": "assistant", "content": None, "tool_calls": [
    {"id": "call_00_a", "type": "function",
     "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}},
    {"id": "call_01_b", "type": "function",
     "function": {"name": "terminal", "arguments": '{"command":"ls"}'}},
]}


def is_legal(history) -> tuple[bool, str]:
    """按 API 规则校验:每个 tool_call 都要有 tool 回复,且不能被插入打断。"""
    pending: set = set()
    for m in history:
        if m.get("role") == "tool":
            cid = m.get("tool_call_id")
            if cid not in pending:
                return False, f"孤儿 tool: {cid}"
            pending.discard(cid)
        else:
            if pending:
                return False, f"未响应就被打断: {pending}"
            if m.get("role") == "assistant":
                pending = {tc.get("id") for tc in (m.get("tool_calls") or [])}
    if pending:
        return False, f"结尾悬空: {pending}"
    return True, "合法"


def _agent(tmp_path, script=()):
    """建一个绑到临时库的 agent(不经 conftest 的 run_script —— 那个会立刻跑一轮)。"""
    store = SessionStore(db_path=str(tmp_path / "arch.db"))
    agent = core.DummyAgent(ScriptedLLM(script), create_default_registry())
    agent.session_store = store
    agent.tools.set_confirm_provider(lambda p: "y")
    return agent, store


class _BatchLLM(ScriptedLLM):
    """第一次主调用返回**同批两个** tool_calls;之后退回脚本行为。"""

    def __init__(self, script=()):
        super().__init__(script)
        self._sent = False

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=4096):
        if tools and not self._sent:
            self._sent = True
            self.main_calls += 1
            return _Msg(tool_calls=[
                _TC(0, "read_file", json.dumps({"path": "a.txt"})),
                _TC(1, "terminal", json.dumps({"command": "ls"})),
            ])
        return super().chat(messages=messages, tools=tools,
                            temperature=temperature, max_tokens=max_tokens)


# ============================================================
# A 组:经 chat() 的退出路径
# ============================================================
def test_AR_chat1_exception_inside_tool_loop(tmp_path):
    """AR-chat1:工具循环体内抛异常 → 落库历史合法,且异常原样抛出。"""
    agent, store = _agent(tmp_path, [
        ("read_file", {"path": "a.txt"}), ("terminal", {"command": "ls"})])

    real = agent.tools.dispatch
    n = {"k": 0}

    def boom(name, args):
        n["k"] += 1
        if n["k"] >= 2:
            raise RuntimeError("模拟循环体内异常")
        return real(name, args)

    agent.tools.dispatch = boom

    raised = None
    try:
        agent.chat("做两件事")
    except Exception as e:
        raised = e
    assert isinstance(raised, RuntimeError), "原始异常必须原样抛出,不能被收口吞掉"

    ok, why = is_legal(store.load_history(agent.current_session_id))
    assert ok, f"经 chat 退出后落库历史非法: {why}"


def test_AR_chat2_normal_finish_is_legal(tmp_path):
    """AR-chat2:正常跑完 → 合法。"""
    agent, store = _agent(tmp_path, [])
    agent.chat("你好")
    ok, why = is_legal(store.load_history(agent.current_session_id))
    assert ok, why


def test_AR_chat3_batch_fully_executed(tmp_path):
    """AR-chat3:一批 tool_calls 全部执行完 → 合法(对照,本应合法)。"""
    agent, store = _agent(tmp_path, [
        ("read_file", {"path": "a.txt"}), ("terminal", {"command": "ls"})])
    agent.chat("做两件事")
    ok, why = is_legal(store.load_history(agent.current_session_id))
    assert ok, why


# ============================================================
# B 组:批次中途停机(★核心判据)
# ============================================================
def test_AR_stop1_kill_mid_batch_disk_must_be_legal(tmp_path):
    """AR-stop1:一批两个 tool_calls,第二个执行时停机。

    **磁盘上**的历史必须合法——要么完整批次,要么停在批次之前。
    绝不能出现"assistant(tool_calls) 已落库、同批 tool 回复只落了一部分"。

    这是"事后修"与"不让发生"的分水岭:
      靠"每条工具结果后落库 + 事后 repair"→ 磁盘上已有半个批次(红)
      批次原子落库 → 非法状态从未写进磁盘(绿)
    """
    agent, store = _agent(tmp_path)
    agent.llm = _BatchLLM()

    real = agent.tools.dispatch
    n = {"k": 0}

    def boom(name, args):
        n["k"] += 1
        if n["k"] >= 2:
            raise RuntimeError("模拟批次中途停机")
        return real(name, args)

    agent.tools.dispatch = boom
    try:
        agent.chat("两个工具一起用")
    except Exception:
        pass                       # 用户视角:外层吞掉异常后继续

    disk = store.load_history(agent.current_session_id)
    ok, why = is_legal(disk)
    assert ok, f"批次中途停机后磁盘历史非法: {why}\n磁盘内容: {disk}"


def test_AR_stop2_kill_at_first_tool_disk_must_be_legal(tmp_path):
    """AR-stop2:一批两个 tool_calls,**第一个**就停机 → 磁盘仍须合法。"""
    agent, store = _agent(tmp_path)
    agent.llm = _BatchLLM()

    def boom(name, args):
        raise RuntimeError("模拟第一个工具就停机")

    agent.tools.dispatch = boom
    try:
        agent.chat("两个工具一起用")
    except Exception:
        pass

    disk = store.load_history(agent.current_session_id)
    ok, why = is_legal(disk)
    assert ok, f"停机后磁盘历史非法: {why}\n磁盘内容: {disk}"


# ============================================================
# C 组:持久层容忍度(参考项,不作为方案验收)
# ============================================================
STORE_SHAPES = {
    "S1_刚入assistant就停机":
        [{"role": "system", "content": "sys"},
         {"role": "user", "content": "做事"}, TWO_CALLS],
    "S2_一批中只写了第一条":
        [{"role": "system", "content": "sys"},
         {"role": "user", "content": "做事"}, TWO_CALLS,
         {"role": "tool", "tool_call_id": "call_00_a", "content": "A"}],
    "S4_尾部悬空后又接了user":
        [{"role": "system", "content": "sys"},
         {"role": "user", "content": "做事"}, TWO_CALLS,
         {"role": "tool", "tool_call_id": "call_00_a", "content": "A"},
         {"role": "assistant", "content": "[合成]"},
         {"role": "user", "content": "接着干"}],
}


@pytest.mark.parametrize("name", list(STORE_SHAPES))
def test_AR_store_tolerance_reference(name, tmp_path):
    """AR-store(参考项):非法历史直接落库 → 读回来**当前**并不合法。

    这不是"系统会不会产生非法历史",而是"持久层对非法输入是否容忍"。
    真实写入者只有 chat(),故本组**不作为任何方案的验收标准**;
    保留它只为记录现状(以及将来若给持久层加校验时作为对照)。
    """
    store = SessionStore(db_path=str(tmp_path / "s.db"))
    sid = store.create_session()
    store.save_history(sid, STORE_SHAPES[name])
    ok, _why = is_legal(store.load_history(sid))
    assert not ok, (
        f"{name} 现在竟然合法了 —— 说明持久层加了校验/修复。"
        "若是有意为之,请更新本参考项并说明;不要静默通过。")


def test_AR5_save_load_roundtrip_is_lossless(tmp_path):
    """AR5:持久层忠实读取 —— save→load 不得增删消息。"""
    src = [{"role": "system", "content": "sys"},
           {"role": "user", "content": "做事"}, TWO_CALLS]
    store = SessionStore(db_path=str(tmp_path / "rt.db"))
    sid = store.create_session()
    store.save_history(sid, src)
    loaded = store.load_history(sid)
    assert [m["role"] for m in loaded] == [m["role"] for m in src], \
        "持久层不得增删消息(忠实读取)"


def test_AR4_compressed_history_legal():
    """AR4:压缩契约不变 —— _validate_history 仍拒绝非法结构。"""
    from compressor import _validate_history
    assert _validate_history([{"role": "system", "content": "s"}])
    assert not _validate_history(
        [{"role": "system", "content": "s"}, TWO_CALLS])   # 悬空 → 拒绝


def test_AR7_clean_history_needs_no_repair():
    """AR7:干净历史零修复动作(修复函数对合法输入必须"无改动")。"""
    from session_store import repair_tool_pairing
    clean = [{"role": "system", "content": "sys"},
             {"role": "user", "content": "hi"},
             {"role": "assistant", "content": "hello"}]
    _, stats = repair_tool_pairing(clean)
    assert stats["total"] == 0, f"干净历史不该有修复动作: {stats}"


# ============================================================
# SOFT 组:磁盘上是否"从未出现过"非法状态(软标准,通过加分)
# ============================================================
def _disk_snapshots(tmp_path, monkeypatch) -> list[tuple[list[dict], str]]:
    """跑一次"批次中途停机",记录**每一次落库时**写入磁盘的历史。

    返回 [(快照, ), ...] —— 用于检查"磁盘上是否短暂出现过非法状态"。
    """
    snapshots: list[list[dict]] = []
    real_save = SessionStore.save_history

    def spying_save(self, session_id, history):
        snapshots.append([dict(m) for m in history])
        return real_save(self, session_id, history)

    monkeypatch.setattr(SessionStore, "save_history", spying_save)

    agent, store = _agent(tmp_path)
    agent.llm = _BatchLLM()

    real_dispatch = agent.tools.dispatch
    n = {"k": 0}

    def boom(name, args):
        n["k"] += 1
        if n["k"] >= 2:
            raise RuntimeError("模拟批次中途停机")
        return real_dispatch(name, args)

    agent.tools.dispatch = boom
    try:
        agent.chat("两个工具一起用")
    except Exception:
        pass

    return [(s, is_legal(s)[1]) for s in snapshots]


def test_SOFT1_disk_never_holds_illegal_state(tmp_path, monkeypatch):
    """SOFT1(软标准):磁盘上每一次落库都快照,其中**不应有非法状态**。

    这是"加分项",不是验收门槛:
      - 通过(所有快照合法)= "不让发生" —— 强杀/断电也安全
      - 不通过(某次快照非法)= "事后修" —— 只要最终状态合法,仍可采纳
    """
    snaps = _disk_snapshots(tmp_path, monkeypatch)
    illegal = [(i, why) for i, (_, why) in enumerate(snaps) if "合法" not in why]

    if illegal:
        pytest.xfail(
            f"软标准未通过:第 {[i for i, _ in illegal]} 次落库时磁盘上是非法状态"
            f"({illegal[0][1]})。说明是'事后修'而非'不让发生' —— "
            "杀掉进程不会执行修复,那时磁盘会留着半个批次。"
            "(仍可采纳,此项仅为加分对照。)")
    assert snaps, "应当发生过落库"

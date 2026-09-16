"""P1a 工具护栏测试集。

分两组:
  - 纯控制器(无副作用):阈值/清零/开关/签名/指纹 —— 不碰 LLM、不碰文件系统
  - 端到端:用假 LLM 驱动 core.chat() 真实工具循环,验证接线与拦截恰好发生
    (假 LLM 与隔离夹具见 tests/conftest.py)

全部 mock,零 API 成本,确定性断言。
另含一条回归:dispatch 不得改写调用方传入的参数 dict
(原实现注入 _confirm 时直接改写,导致护栏前后签名不一致而失效)。
"""

import core
from conftest import ScriptedLLM, pairing_ok, tool_contents

from tool_guardrails import (
    GuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
    append_guidance,
    _result_hash,
)
from tools import create_default_registry


# ============================================================
# 纯控制器
# ============================================================
FAIL = "[错误] 文件不存在: nope.txt"
ARGS = {"path": "nope.txt"}
SAME = "line1\nline2"


def test_exact_failure_warns_then_pauses():
    """完全相同调用反复失败:第 2 次告警,第 6 次拦截(即 5 次执行后)。"""
    c = ToolCallGuardrailController()
    actions = []
    for i in range(1, 7):
        if not c.before_call("read_file", ARGS).allows_execution:
            actions.append(("pause", i))
            break
        actions.append((c.after_call("read_file", ARGS, FAIL).action, i))
    assert actions[1] == ("warn", 2)
    assert actions[4][1] == 5, "第5次应仍放行"
    assert actions[-1] == ("pause", 6)


def test_varying_failures_are_not_a_loop():
    """同命令但**报错每次都不同** → 不算打转(这就是"改一处、跑一次"的迭代)。

    回归:判据曾只看"工具+参数",把这种合法迭代判成原地打转并在第 6 次拦死;
    而验证门要求"跑通全量测试才算证据",两者于是互锁 —— 实测结局是驳回两次后
    以"无证据 + 声称完成"收尾,正是验证门要防的事。
    """
    c = ToolCallGuardrailController()
    args = {"command": "python -m pytest tests/ -q"}
    for i in range(1, 9):
        assert c.before_call("terminal", args).allows_execution, f"第 {i} 次不该被拦"
        d = c.after_call("terminal", args, f"[EXIT CODE: 1]\n{i} failed, 180 passed")
        assert d.action == "allow", f"第 {i} 次报错不同,不该告警"


def test_identical_failure_restarts_count_then_pauses():
    """报错变了就重新计数;此后**连续同一份失败**满 5 次仍会被拦。"""
    c = ToolCallGuardrailController()
    args = {"command": "python -m pytest tests/ -q"}
    c.after_call("terminal", args, "[EXIT CODE: 1]\n失败A")     # 计数从 1 重新开始
    for _ in range(5):
        c.after_call("terminal", args, "[EXIT CODE: 1]\n失败B")  # 同一份失败
    assert not c.before_call("terminal", args).allows_execution


def test_success_resets_failure_counter():
    """成功一次即清零:避免"先失败→修好→再失败"被累计误判。"""
    c = ToolCallGuardrailController()
    for _ in range(4):
        c.after_call("read_file", ARGS, FAIL)
    c.after_call("read_file", ARGS, "正常内容")
    assert c.before_call("read_file", ARGS).action == "allow"


def test_different_args_are_independent():
    """参数不同即不同签名,互不牵连。"""
    c = ToolCallGuardrailController()
    for _ in range(5):
        c.after_call("read_file", {"path": "a.txt"}, FAIL)
    assert c.before_call("read_file", {"path": "b.txt"}).action == "allow"
    assert c.before_call("read_file", {"path": "a.txt"}).action == "pause"


def test_idempotent_no_progress_warns_then_pauses():
    """只读工具同结果重复:第 2 次告警,第 6 次拦截。"""
    c = ToolCallGuardrailController()
    actions = []
    for i in range(1, 7):
        if not c.before_call("read_file", {"path": "x.txt"}).allows_execution:
            actions.append(("pause", i))
            break
        actions.append((c.after_call("read_file", {"path": "x.txt"}, SAME).action, i))
    assert actions[1] == ("warn", 2)
    assert actions[-1] == ("pause", 6)


def test_mutating_tool_never_tracked_for_no_progress():
    """有副作用工具不参与无进展判定(重试可能有合法意图)。"""
    c = ToolCallGuardrailController()
    for _ in range(6):
        c.after_call("terminal", {"command": "ls"}, SAME)
    assert c.before_call("terminal", {"command": "ls"}).action == "allow"


def test_changing_result_is_progress():
    """结果每次都不同 → 不算无进展。"""
    c = ToolCallGuardrailController()
    for j in range(6):
        c.after_call("read_file", {"path": "y.txt"}, f"内容{j}")
    assert c.before_call("read_file", {"path": "y.txt"}).action == "allow"


def test_reset_for_turn_clears_counters():
    """护栏按轮统计,换轮清零。"""
    c = ToolCallGuardrailController()
    for _ in range(5):
        c.after_call("read_file", ARGS, FAIL)
    c.reset_for_turn()
    assert c.before_call("read_file", ARGS).action == "allow"


def test_hard_stop_disabled_only_warns():
    """关掉硬停 → 永不拦截(只告警)。"""
    c = ToolCallGuardrailController(GuardrailConfig(hard_stop_enabled=False))
    for _ in range(8):
        c.after_call("read_file", ARGS, FAIL)
    assert c.before_call("read_file", ARGS).action == "allow"


def test_warnings_disabled_produces_no_warn():
    """关掉告警 → 不产生 warn。"""
    c = ToolCallGuardrailController(GuardrailConfig(warnings_enabled=False))
    c.after_call("read_file", ARGS, FAIL)
    assert c.after_call("read_file", ARGS, FAIL).action == "allow"


def test_signature_is_key_order_insensitive():
    """参数键序不影响签名。"""
    assert ToolCallSignature.from_call("read_file", {"a": 1, "b": 2}) == \
        ToolCallSignature.from_call("read_file", {"b": 2, "a": 1})


def test_result_hash_treats_json_semantically_equal():
    """JSON 键序不同但语义相同 → 视为同一结果。"""
    assert _result_hash('{"a":1,"b":2}') == _result_hash('{"b": 2, "a": 1}')


def test_report_text_markers():
    """护栏报告应带"[护栏提示]"前缀,且不含判决语气(不替模型下结论)。"""
    c = ToolCallGuardrailController()
    d = c.after_call("read_file", ARGS, FAIL)          # 第 1 次:不报
    assert d.action == "allow"
    d = c.after_call("read_file", ARGS, FAIL)          # 第 2 次达阈值 → 报告
    assert d.action == "warn"
    report = c.build_report(d)
    assert "[护栏提示]" in report
    for banned in ("不要", "禁止", "不许", "判定为"):
        assert banned not in report, f"报告里不该有判决措辞: {banned}"



def _pauses(history):
    return [c for c in tool_contents(history) if "[护栏提示]" in c]

def _contexts(history):
    return [m for m in history if m.get("role") == "user"
            and "[本轮运行情况]" in str(m.get("content"))]



def _warns(history):
    """同 _pauses —— warn 与 pause 现在用同一种报告格式("[护栏提示]")。"""
    return _pauses(history)


def test_e2e_exact_failure_pauses_after_five(run_script, tmp_path):
    """同一调用反复真失败:5 次执行 + 第 6 次拦截,循环随后正常收尾。"""
    missing = str(tmp_path / "nope.txt")
    agent, llm = run_script([("read_file", {"path": missing})] * 6)
    history = agent.history

    assert len([c for c in tool_contents(history) if "[错误]" in c]) == 5
    # 新语义:从第 2 次起每次失败都追加报告(不再"只在第 6 次拦一次")
    assert len(_pauses(history)) >= 4
    assert len(_warns(history)) >= 1, "第 2 次失败起应注入报告"
    assert llm.main_calls == 8, "报告后应还能继续到模型给出文本(含一轮情况说明)"


def test_e2e_fix_run_iteration_not_blocked(tmp_path, monkeypatch):
    """改 .py → 连跑测试(每次报错都不同)→ 最后跑绿:零拦截,且证据成立。

    这是"互锁"冲突的端到端回归:护栏若拦掉取证命令,验证门就永远拿不到证据。
    终端结果用假数据喂(真跑 5 遍套件太慢),所以不走 run_script 夹具
    (它会立刻真跑一遍),自己建 agent 并替换 dispatch。
    """
    real_store = core.SessionStore
    monkeypatch.setattr(
        core, "SessionStore",
        lambda *a, **k: real_store(db_path=str(tmp_path / "t.db")))

    runs = [f"[SHELL: bash]\n{i} failed, 180 passed\n"
            f"FAILED tests/test_a.py::test_{i}\n[EXIT CODE: 1]"
            for i in range(1, 5)]
    runs.append("[SHELL: bash]\n186 passed in 13.2s")      # 修好了
    script = ([("write_file", {"path": str(tmp_path / "app.py"),
                              "content": "x = 1\n", "verify": False})]
              + [("terminal", {"command": "python -m pytest tests/ -q"})] * len(runs))

    agent = core.DummyAgent(ScriptedLLM(script), create_default_registry())
    agent.tools.set_confirm_provider(lambda prompt: "y")

    remaining = iter(runs)
    real_dispatch = agent.tools.dispatch
    agent.tools.dispatch = lambda name, args: (
        next(remaining) if name == "terminal" else real_dispatch(name, args))

    agent.chat("修好测试")

    assert _pauses(agent.history) == [], "正常迭代不该被拦"
    # 跑绿之后应有一份情况说明(含执行记录),而不是"通过证据"这种判决
    ctxs = [m for m in agent.history if m.get("role") == "user"
            and "[本轮运行情况]" in str(m.get("content"))]
    assert ctxs, "应附上本轮情况说明"
    assert "pytest" in ctxs[0]["content"], "说明里应带上跑过的测试命令"


def test_e2e_blocked_still_gets_context(tmp_path, monkeypatch):
    """护栏拦掉命令(同一份失败连续 5 次)→ 仍拦;情况说明照常附上。

    回归:原先护栏拦掉取证命令会让验证门陷入"门要求跑通、护栏禁止跑"的
    互锁。现在门不判决了,只把情况(含被拦这件事)摆给模型自己判断。
    """
    real_store = core.SessionStore
    monkeypatch.setattr(
        core, "SessionStore",
        lambda *a, **k: real_store(db_path=str(tmp_path / "t.db")))

    same = ("[SHELL: bash]\n1 failed, 180 passed\n"
            "FAILED tests/test_a.py::test_x\n[EXIT CODE: 1]")
    script = ([("write_file", {"path": str(tmp_path / "app.py"),
                              "content": "x = 1\n", "verify": False})]
              + [("terminal", {"command": "python -m pytest tests/ -q"})] * 6)
    agent = core.DummyAgent(ScriptedLLM(script), create_default_registry())
    agent.tools.set_confirm_provider(lambda prompt: "y")

    real_dispatch = agent.tools.dispatch
    agent.tools.dispatch = lambda name, args: (
        same if name == "terminal" else real_dispatch(name, args))

    result = agent.chat("修好测试")

    blocks = [c for c in tool_contents(agent.history) if "[护栏提示]" in c]
    assert len(blocks) >= 4, "同一份失败反复出现 → 每轮都报告(不再拦截到底)"
    assert _contexts(agent.history), "被拦的事实应出现在情况说明里"
    assert result, "仍以回答收尾"


def test_e2e_idempotent_no_progress_pauses(run_script, tmp_path):
    """同一只读调用返回同内容:5 次成功 + 第 6 次拦截。"""
    same = tmp_path / "same.txt"
    same.write_text("固定内容\n", encoding="utf-8")
    agent, _ = run_script([("read_file", {"path": str(same)})] * 6)

    assert len([c for c in tool_contents(agent.history) if "固定内容" in c]) == 5
    blocks = _pauses(agent.history)
    assert len(blocks) >= 4, "重复的只读调用应反复报告(不再拦一次就止)"
    assert "完全相同的结果" in blocks[-1]


def test_e2e_normal_flow_untouched(run_script, tmp_path):
    """正常多步流程(参数各异)零拦截零告警。"""
    paths = []
    for k in range(4):
        p = tmp_path / f"f{k}.txt"
        p.write_text(f"内容{k}\n", encoding="utf-8")
        paths.append(str(p))
    agent, llm = run_script([("read_file", {"path": p}) for p in paths])

    assert _pauses(agent.history) == []
    assert _warns(agent.history) == []
    assert llm.main_calls == 6, "4 次读取 + 收尾 + 一轮情况说明"


def test_e2e_tool_pairing_still_valid(run_script, tmp_path):
    """护栏不破坏 assistant(tool_calls) ↔ tool 回复的配对约束。"""
    missing = str(tmp_path / "nope.txt")
    agent, _ = run_script([("read_file", {"path": missing})] * 6)

    ok, why = pairing_ok(agent.history)
    assert ok, why


# ============================================================
# 回归:dispatch 不得改写调用方参数
# ============================================================
def test_dispatch_does_not_mutate_caller_args(tmp_path):
    """dispatch 注入 _confirm 时必须拷贝,不能污染调用方的 dict。

    这是护栏能生效的前提:若被污染,"调用前"与"调用后"会算出两个不同签名,
    重复失败计数永远对不上,护栏形同失效(2026-09-14 实测踩到)。
    """
    reg = create_default_registry()
    reg.set_confirm_provider(lambda prompt: "")
    args = {"path": str(tmp_path / "missing.txt")}

    reg.dispatch("read_file", args)

    assert set(args.keys()) == {"path"}, f"参数被污染: {list(args.keys())}"

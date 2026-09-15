"""P1a 工具护栏测试集。

分两组:
  - 纯控制器(无副作用):阈值/清零/开关/签名/指纹 —— 不碰 LLM、不碰文件系统
  - 端到端:用假 LLM 驱动 core.chat() 真实工具循环,验证接线与拦截恰好发生
    (假 LLM 与隔离夹具见 tests/conftest.py)

全部 mock,零 API 成本,确定性断言。
另含一条回归:dispatch 不得改写调用方传入的参数 dict
(原实现注入 _confirm 时直接改写,导致护栏前后签名不一致而失效)。
"""

from conftest import pairing_ok, tool_contents

from tool_guardrails import (
    GuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
    append_guidance,
    synthetic_result,
    _result_hash,
)
from tools import create_default_registry


# ============================================================
# 纯控制器
# ============================================================
FAIL = "[错误] 文件不存在: nope.txt"
ARGS = {"path": "nope.txt"}
SAME = "line1\nline2"


def test_exact_failure_warns_then_blocks():
    """完全相同调用反复失败:第 2 次告警,第 6 次拦截(即 5 次执行后)。"""
    c = ToolCallGuardrailController()
    actions = []
    for i in range(1, 7):
        if not c.before_call("read_file", ARGS).allows_execution:
            actions.append(("block", i))
            break
        actions.append((c.after_call("read_file", ARGS, FAIL).action, i))
    assert actions[1] == ("warn", 2)
    assert actions[4][1] == 5, "第5次应仍放行"
    assert actions[-1] == ("block", 6)


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
    assert c.before_call("read_file", {"path": "a.txt"}).action == "block"


def test_idempotent_no_progress_warns_then_blocks():
    """只读工具同结果重复:第 2 次告警,第 6 次拦截。"""
    c = ToolCallGuardrailController()
    actions = []
    for i in range(1, 7):
        if not c.before_call("read_file", {"path": "x.txt"}).allows_execution:
            actions.append(("block", i))
            break
        actions.append((c.after_call("read_file", {"path": "x.txt"}, SAME).action, i))
    assert actions[1] == ("warn", 2)
    assert actions[-1] == ("block", 6)


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


def test_synthetic_and_guidance_text_markers():
    """拦截结果 / 告警文本的标记可辨识;非 warn 不改动结果。"""
    c = ToolCallGuardrailController()
    for _ in range(5):
        c.after_call("read_file", ARGS, FAIL)
    assert "[护栏拦截]" in synthetic_result(c.before_call("read_file", ARGS))

    c2 = ToolCallGuardrailController()
    c2.after_call("read_file", ARGS, FAIL)
    warn = c2.after_call("read_file", ARGS, FAIL)
    assert "[工具循环告警:" in append_guidance("原始结果", warn)
    assert append_guidance("原始结果", c2.before_call("read_file", ARGS)) == "原始结果"


# ============================================================
# 端到端(假 LLM 驱动真实循环,夹具见 conftest.py)
# ============================================================
def _blocks(history):
    return [c for c in tool_contents(history) if "[护栏拦截]" in c]


def _warns(history):
    return [c for c in tool_contents(history) if "[工具循环告警" in c]


def test_e2e_exact_failure_blocks_after_five(run_script, tmp_path):
    """同一调用反复真失败:5 次执行 + 第 6 次拦截,循环随后正常收尾。"""
    missing = str(tmp_path / "nope.txt")
    agent, llm = run_script([("read_file", {"path": missing})] * 6)
    history = agent.history

    assert len([c for c in tool_contents(history) if "[错误]" in c]) == 5
    assert len(_blocks(history)) == 1
    assert len(_warns(history)) >= 1, "第 2 次失败起应注入告警"
    assert llm.main_calls == 7, "被拦后应还能继续到模型给出文本"


def test_e2e_idempotent_no_progress_blocks(run_script, tmp_path):
    """同一只读调用返回同内容:5 次成功 + 第 6 次拦截。"""
    same = tmp_path / "same.txt"
    same.write_text("固定内容\n", encoding="utf-8")
    agent, _ = run_script([("read_file", {"path": str(same)})] * 6)

    assert len([c for c in tool_contents(agent.history) if "固定内容" in c]) == 5
    blocks = _blocks(agent.history)
    assert len(blocks) == 1
    assert "无进展" in blocks[0]


def test_e2e_normal_flow_untouched(run_script, tmp_path):
    """正常多步流程(参数各异)零拦截零告警。"""
    paths = []
    for k in range(4):
        p = tmp_path / f"f{k}.txt"
        p.write_text(f"内容{k}\n", encoding="utf-8")
        paths.append(str(p))
    agent, llm = run_script([("read_file", {"path": p}) for p in paths])

    assert _blocks(agent.history) == []
    assert _warns(agent.history) == []
    assert llm.main_calls == 5


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

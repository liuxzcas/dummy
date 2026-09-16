"""护栏测试 —— 纯观察者语义(只报告,从不拦截)。

============ 语义变更史 ============
  block(第 N 次起不执行) → pause(拦下这一次) → **report(从不拦,只添一句事实)**

所以本文件的断言围绕三件事:
  1. 计数正确(同工具+同参数+**同结果**才累计)
  2. 报告只在**恰好达到阈值**时给出一次
  3. **从不拦截** —— allows_execution 恒为 True
"""
import pytest

from tool_guardrails import (
    GuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
    append_guidance,
    _result_hash,
)

ARGS = {"path": "a.txt"}
FAIL = "[错误] 文件不存在: a.txt"
SAME = "1|固定内容"


# ============================================================
# 计数与报告阈值
# ============================================================
def test_same_failure_counts_up_and_reports_at_threshold():
    """同一份失败(同工具+同参数+同报错)连续出现 → 达到阈值时报告一次。"""
    c = ToolCallGuardrailController()
    cfg = c.config
    actions = []
    for i in range(1, cfg.exact_failure_report_after + 1):
        d = c.after_call("read_file", ARGS, FAIL)
        actions.append((i, d.action, d.count))
    # 第 1..阈值-1 不报,第阈值 次报
    assert actions[0][1] == "allow"
    assert actions[-1][1] == "report"
    assert actions[-1][2] == cfg.exact_failure_report_after


def test_report_only_once_at_threshold():
    """只在恰好达到阈值时报一次(不是之后每次都报,避免刷屏)。"""
    c = ToolCallGuardrailController()
    n = c.config.exact_failure_report_after
    actions = [c.after_call("read_file", ARGS, FAIL).action for _ in range(n + 4)]
    assert actions.count("report") == 1, f"应只报一次,实际 {actions}"


def test_different_failure_does_not_accumulate():
    """报错内容不同 → 不算重复(正常迭代不该被当成打转)。"""
    c = ToolCallGuardrailController()
    for i in range(8):
        d = c.after_call("read_file", ARGS, f"{FAIL} (第 {i} 次不同)")
        assert d.action == "allow", "报错每次不同 → 不该报告"


def test_different_args_are_independent():
    """不同参数的计数互不影响。"""
    c = ToolCallGuardrailController()
    n = c.config.exact_failure_report_after
    for _ in range(n):
        c.after_call("read_file", {"path": "b.txt"}, FAIL)
    assert c.after_call("read_file", ARGS, FAIL).action == "allow"


def test_success_clears_failure_counter():
    """成功一次 → 该签名的失败计数清零(修好后再失败不该累计)。"""
    c = ToolCallGuardrailController()
    n = c.config.exact_failure_report_after
    for _ in range(n - 1):
        c.after_call("read_file", ARGS, FAIL)
    c.after_call("read_file", ARGS, "成功内容")
    assert c.after_call("read_file", ARGS, FAIL).action == "allow"


def test_reset_for_turn_clears_counters():
    """每轮开头重置计数。"""
    c = ToolCallGuardrailController()
    n = c.config.exact_failure_report_after
    for _ in range(n):
        c.after_call("read_file", ARGS, FAIL)
    c.reset_for_turn()
    assert c.after_call("read_file", ARGS, FAIL).action == "allow"


# ============================================================
# 只读工具"无进展"
# ============================================================
def test_idempotent_no_progress_reports_at_threshold():
    """只读工具连续返回相同结果 → 达到阈值时报告。"""
    c = ToolCallGuardrailController()
    n = c.config.no_progress_report_after
    actions = [c.after_call("read_file", {"path": "x.txt"}, SAME).action
               for _ in range(n)]
    assert actions.count("report") == 1


def test_changing_result_is_progress():
    """结果在变 → 有进展,不报告。"""
    c = ToolCallGuardrailController()
    for i in range(8):
        d = c.after_call("read_file", {"path": "x.txt"}, f"{SAME} 第{i}版")
        assert d.action == "allow"


def test_mutating_tool_never_tracked_for_no_progress():
    """有副作用工具不参与"无进展"统计(写文件重复是正常的)。"""
    c = ToolCallGuardrailController()
    for _ in range(8):
        d = c.after_call("write_file", {"path": "a.txt"}, "写入成功")
        assert d.action == "allow"


# ============================================================
# 核心不变量:从不拦截
# ============================================================
@pytest.mark.parametrize("n_calls", [1, 3, 6, 12, 30])
def test_never_blocks_any_call(n_calls):
    """**无论重复多少次,护栏都不阻止执行** —— 这是它作为"纯观察者"的定义。"""
    c = ToolCallGuardrailController()
    for _ in range(n_calls):
        d = c.after_call("read_file", ARGS, FAIL)
        assert d.allows_execution is True
        assert d.action in {"allow", "report"}


# ============================================================
# 开关与配置
# ============================================================
def test_reports_can_be_disabled():
    """reports_enabled=False → 完全不报告(计数仍在,但不打扰)。"""
    c = ToolCallGuardrailController(GuardrailConfig(reports_enabled=False))
    n = c.config.exact_failure_report_after + 3
    for _ in range(n):
        assert c.after_call("read_file", ARGS, FAIL).action == "allow"


def test_threshold_is_configurable():
    """阈值可配(默认 6)。"""
    c = ToolCallGuardrailController(GuardrailConfig(exact_failure_report_after=2))
    assert c.after_call("read_file", ARGS, FAIL).action == "allow"
    assert c.after_call("read_file", ARGS, FAIL).action == "report"


# ============================================================
# 报告文案:陈述事实,不下判决
# ============================================================
def test_build_report_states_facts_not_verdicts():
    """报告必须是**事实陈述**,不能含禁止性措辞(那是替模型做判断)。"""
    c = ToolCallGuardrailController()
    n = c.config.exact_failure_report_after
    for _ in range(n):
        d = c.after_call("read_file", ARGS, FAIL)
    report = c.build_report(d, extra_facts=["这期间你改动过 1 个文件"])
    assert "[护栏提示]" in report
    assert "连续相同次数" in report
    assert "这期间你改动过 1 个文件" in report
    for banned in ("不要", "禁止", "不许", "判定为"):
        assert banned not in report, f"报告不该含判决措辞: {banned}"


def test_append_guidance_attaches_report():
    """报告通过 append_guidance 追加到工具结果末尾(模型能看到)。"""
    c = ToolCallGuardrailController()
    n = c.config.exact_failure_report_after
    for _ in range(n):
        d = c.after_call("read_file", ARGS, FAIL)
    out = append_guidance("原始结果", c.build_report(d))
    assert out.startswith("原始结果")
    assert "[护栏提示]" in out


def test_append_guidance_noop_without_report():
    """报告文本为空时不改动原结果。"""
    assert append_guidance("原始结果", "") == "原始结果"


# ============================================================
# 端到端(走真实 chat 循环)
# ============================================================
def _reports(history):
    """历史里带护栏报告的工具结果。"""
    return [m for m in history if m.get("role") == "tool"
            and "[护栏提示]" in str(m.get("content"))]


def test_e2e_repeated_failure_still_executes(run_script, tmp_path):
    """连续 6 次相同失败 → 每次都真的执行了(不拦截),报告出现一次。"""
    from conftest import tool_contents
    agent, llm = run_script([("read_file", {"path": str(tmp_path / "missing")})] * 6)

    contents = tool_contents(agent.history)
    assert len([c for c in contents if "[错误]" in c]) == 6, "6 次都该真的执行"
    assert len(_reports(agent.history)) == 1, "只报一次"


def test_e2e_report_reaches_model_as_fact(run_script, tmp_path):
    """报告内容要包含可判断的事实(次数 + 是否改过文件)。"""
    agent, _ = run_script([("read_file", {"path": str(tmp_path / "missing")})] * 6)
    rep = str(_reports(agent.history)[0]["content"])
    assert "连续相同次数" in rep
    assert "改动" in rep

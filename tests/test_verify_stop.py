"""P1b 收尾前验证门测试集(档 A 规格见 docs/p1b-verification-design.md)。

分两组:
  - 纯账本/判据:什么算"可验证路径"、什么算"改动"、什么算"全量测试通过证据"
  - 端到端:假 LLM 驱动真实循环,验证"改代码就想收工"确实被驳回、有证据则放行

档 A 核心:证据 = 一次**全量测试套件**的 pytest 执行且成功,且必须晚于最后一次编辑。
"""

import os

from verify_stop import (
    VerificationLedger,
    is_evidence_command,
    is_full_suite_pytest_command,
    is_pytest_evidence,
    is_verifiable_path,
    is_write_success,
)

PY = {"path": "D:/proj/app.py"}
MD = {"path": "D:/proj/README.md"}
WRITE_OK = "[写入]成功: D:/proj/app.py (3 行, 42 字符)"
WRITE_CANCEL = "[用户拒绝] 追加已取消"
WRITE_SKIP = "[跳过] 文件内容无变化"
WRITE_ERR = "[错误] 无权限写入: D:/proj/app.py"
PYTEST_OK = "[SHELL: bash]\n153 passed in 9.0s"
PYTEST_FAIL = "[SHELL: bash]\n1 failed\n[EXIT CODE: 1]"
FULL = "python -m pytest tests/ -q"


# ============================================================
# 判据
# ============================================================
def test_verifiable_path_excludes_docs():
    """文档类不参与验证(改 README 不该被要求跑测试)。"""
    assert is_verifiable_path("a.py") is True
    assert is_verifiable_path("a.md") is False
    assert is_verifiable_path("a.txt") is False
    assert is_verifiable_path("data.csv") is False
    assert is_verifiable_path("LICENSE") is False
    assert is_verifiable_path("CHANGELOG.md") is False
    assert is_verifiable_path("noext") is True, "无扩展名且非文档名 → 视为可验证"
    assert is_verifiable_path(None) is False
    assert is_verifiable_path("") is False


def test_write_success_detection():
    """只有真写入才算改动:取消/跳过/报错都不算。"""
    assert is_write_success(WRITE_OK) is True
    assert is_write_success(WRITE_CANCEL) is False
    assert is_write_success(WRITE_SKIP) is False
    assert is_write_success(WRITE_ERR) is False
    assert is_write_success("") is False


def test_full_suite_pytest_detection():
    """全量套件判据:以 pytest 调用收尾,且不缩小范围。"""
    assert is_full_suite_pytest_command("python -m pytest tests/ -q") is True
    assert is_full_suite_pytest_command("pytest") is True
    assert is_full_suite_pytest_command("cd /d/x && python -m pytest tests/ -q") is True
    assert is_full_suite_pytest_command("python -m pytest -q") is True
    assert is_full_suite_pytest_command("python -m pytest tests/ -q 2>&1") is True
    assert is_full_suite_pytest_command("PYTHONPATH=. python -m pytest tests/ -q") is True
    assert is_full_suite_pytest_command(".venv/bin/pytest tests/ -q") is True

    assert is_full_suite_pytest_command("pytest tests/test_a.py") is False, "单文件"
    assert is_full_suite_pytest_command("pytest tests/test_a.py::test_x") is False, "单用例"
    assert is_full_suite_pytest_command("python -m pytest -k foo") is False, "缩小范围"
    assert is_full_suite_pytest_command("python -m pytest --lf") is False, "只跑上次失败"
    assert is_full_suite_pytest_command("python -m pytest -m slow") is False, "标记筛选"
    assert is_full_suite_pytest_command("python hello.py") is False, "与 pytest 无关"
    assert is_full_suite_pytest_command(None) is False


def test_pytest_must_be_the_last_shell_segment():
    """必须**以** pytest 调用收尾——否则报告的退出码不是 pytest 的。

    回归背景(2026-09-15 实测):`pytest … | tail` 的退出码是 tail 的(恒为 0),
    会把**失败的测试记成通过**。这条曾真实存在于实现里。
    """
    assert is_full_suite_pytest_command("python -m pytest tests/ -q | tail -3") is False
    assert is_full_suite_pytest_command("python -m pytest tests/ -q 2>&1 | tail -3") is False
    assert is_full_suite_pytest_command("python -m pytest tests/ -q; echo done") is False
    assert is_full_suite_pytest_command("python -m pytest tests/ -q && echo ok") is False
    # 但 `cd … && pytest` 是合法的:pytest 仍是最后一段,退出码就是它的
    assert is_full_suite_pytest_command("cd /d/x && pytest tests/ -q") is True


def test_pytest_must_be_actually_invoked():
    """把 pytest 当普通参数不算调用(避免 `echo pytest` 蒙混)。"""
    assert is_full_suite_pytest_command("echo pytest") is False
    assert is_full_suite_pytest_command("grep pytest README.md") is False
    assert is_full_suite_pytest_command("cat pytest.log") is False


# ============================================================
# 账本
# ============================================================
def _ledger(**kw):
    return VerificationLedger(**kw)


def test_no_edit_no_nudge():
    """本轮没改文件 → 放行。"""
    lg = _ledger()
    assert lg.build_stop_nudge() is None


def test_doc_only_edit_no_nudge():
    """只改文档 → 放行。"""
    lg = _ledger()
    lg.note_tool_result("write_file", MD, WRITE_OK)
    assert lg.build_stop_nudge() is None


def test_cancelled_write_is_not_edit():
    """取消/跳过的写入不算改动 → 放行。"""
    lg = _ledger()
    lg.note_tool_result("write_file", PY, WRITE_CANCEL)
    lg.note_tool_result("write_file", PY, WRITE_SKIP)
    assert lg.build_stop_nudge() is None


def test_code_edit_without_evidence_nudges():
    """改代码且无证据 → 驳回,且文本说清要跑全量套件。"""
    lg = _ledger()
    lg.note_tool_result("write_file", PY, WRITE_OK)
    nudge = lg.build_stop_nudge()
    assert nudge is not None
    assert "完整测试套件" in nudge and "python -m pytest" in nudge
    assert "(第 1/2 次提醒" in nudge
    assert "`D:/proj/app.py`" in nudge


def test_full_suite_pass_is_evidence():
    """跑过全量 pytest 且成功 → 放行。"""
    lg = _ledger()
    lg.note_tool_result("write_file", PY, WRITE_OK)
    lg.note_tool_result("terminal", {"command": FULL}, PYTEST_OK)
    assert lg.build_stop_nudge() is None


def test_failing_pytest_is_not_evidence():
    """pytest 跑失败了不算证据 → 驳回。"""
    lg = _ledger()
    lg.note_tool_result("write_file", PY, WRITE_OK)
    lg.note_tool_result("terminal", {"command": FULL}, PYTEST_FAIL)
    assert lg.build_stop_nudge() is not None


def test_narrow_pytest_is_not_evidence():
    """只跑单个测试文件不算证据(覆盖不到改动) → 驳回。"""
    lg = _ledger()
    lg.note_tool_result("write_file", PY, WRITE_OK)
    lg.note_tool_result("terminal", {"command": "pytest tests/test_a.py"}, PYTEST_OK)
    assert lg.build_stop_nudge() is not None


def test_unrelated_script_is_not_evidence():
    """跑与改动无关的脚本不算证据 → 驳回。"""
    lg = _ledger()
    lg.note_tool_result("write_file", PY, WRITE_OK)
    lg.note_tool_result("terminal", {"command": "python scratch.py"}, "[SHELL: bash]\nok")
    assert lg.build_stop_nudge() is not None


def test_cancelled_pytest_is_not_evidence():
    """被取消、从未执行的 pytest 不算证据(回归)。

    terminal 取消时返回 "[用户取消] 命令未执行"——它不含错误特征,若只看
    is_tool_error 会被误判成"跑过了通过"。所以必须要求 [SHELL: 标记。
    """
    lg = _ledger()
    lg.note_tool_result("write_file", PY, WRITE_OK)
    lg.note_tool_result("terminal", {"command": FULL}, "[用户取消] 命令未执行")
    assert lg.build_stop_nudge() is not None
    assert is_pytest_evidence(FULL, "[用户取消] 命令未执行") is False
    assert is_pytest_evidence(FULL, PYTEST_OK) is True, "[SHELL: 标记下才算真跑过"


def test_piped_pytest_is_not_evidence():
    """管道/后续命令会掩盖退出码 → 不作为证据(回归)。

    `pytest … | tail` 的退出码是 tail 的(恒 0),失败也会显示"成功"。
    """
    piped = "python -m pytest tests/ -q 2>&1 | tail -3"
    lg = _ledger()
    lg.note_tool_result("write_file", PY, WRITE_OK)
    lg.note_tool_result("terminal", {"command": piped}, "[SHELL: bash]\nno tests ran")
    assert lg.build_stop_nudge() is not None

    tailed = "python -m pytest tests/ -q; echo done"
    lg2 = _ledger()
    lg2.note_tool_result("write_file", PY, WRITE_OK)
    lg2.note_tool_result("terminal", {"command": tailed}, "[SHELL: bash]\ndone")
    assert lg2.build_stop_nudge() is not None


def test_evidence_before_edit_is_stale():
    """先跑测试、后改代码 → 证据过期 → 驳回。"""
    lg = _ledger()
    lg.note_tool_result("terminal", {"command": FULL}, PYTEST_OK)
    lg.note_tool_result("write_file", PY, WRITE_OK)
    nudge = lg.build_stop_nudge()
    assert nudge is not None
    assert "已过期" in nudge


def test_attempts_capped():
    """驳回有上限:默认 2 次后无条件放行(防死循环)。"""
    lg = _ledger()
    lg.note_tool_result("write_file", PY, WRITE_OK)
    first, second = lg.build_stop_nudge(), lg.build_stop_nudge()
    assert first is not None and "(第 1/2 次提醒" in first
    assert second is not None and "(第 2/2 次提醒" in second
    assert lg.build_stop_nudge() is None


def test_disabled_never_nudges():
    """关掉验证门 → 放行。"""
    lg = _ledger(enabled=False)
    lg.note_tool_result("write_file", PY, WRITE_OK)
    assert lg.build_stop_nudge() is None


def test_reset_for_turn_clears_state():
    """换轮清零:上一轮的改动与计数不该牵连这一轮。"""
    lg = _ledger()
    lg.note_tool_result("write_file", PY, WRITE_OK)
    lg.reset_for_turn()
    assert lg.build_stop_nudge() is None


def test_many_paths_are_folded():
    """改动文件多时折叠显示,避免驳回消息过长。"""
    lg = _ledger()
    for i in range(10):
        lg.note_tool_result("write_file", {"path": f"D:/p/f{i}.py"}, WRITE_OK)
    nudge = lg.build_stop_nudge()
    assert "另有 2 个" in nudge


def test_from_env_switches():
    """环境变量可关掉验证门、可改上限。"""
    os.environ["DUMMY_VERIFY_ON_STOP"] = "0"
    try:
        assert VerificationLedger.from_env().enabled is False
    finally:
        del os.environ["DUMMY_VERIFY_ON_STOP"]

    os.environ["DUMMY_VERIFY_MAX_ATTEMPTS"] = "4"
    try:
        lg = VerificationLedger.from_env()
        assert lg.max_attempts == 4
        lg.note_tool_result("write_file", PY, WRITE_OK)
        for _ in range(3):
            assert lg.build_stop_nudge() is not None
    finally:
        del os.environ["DUMMY_VERIFY_MAX_ATTEMPTS"]


# ============================================================
# 取证路径被护栏阻断(与 P1a 的交叉点)
# ============================================================
def test_is_evidence_command():
    """只有"全量 pytest 运行"才算取证路径。"""
    assert is_evidence_command("terminal", {"command": "python -m pytest tests/ -q"})
    assert not is_evidence_command("terminal", {"command": "ls -la"})
    assert not is_evidence_command("terminal", {"command": "pytest tests/test_a.py"})
    assert not is_evidence_command("read_file", {"path": "a.py"})
    assert not is_evidence_command("terminal", None)


def test_blocked_evidence_command_suppresses_nudge():
    """取证命令被护栏拦掉 → 不再驳回:本轮已无法验证。

    回归:护栏拦掉全量测试后,模型**怎么都拿不到证据**,门却继续驳回,
    逼它去执行一条被禁止的命令 —— 实测以"无证据 + 声称完成"收尾。
    被阻断属外部原因,门应放行。
    """
    lg = _ledger()
    lg.note_tool_result("write_file", PY, WRITE_OK)
    lg.note_blocked("terminal", {"command": "python -m pytest tests/ -q"})
    assert lg.build_stop_nudge() is None


def test_blocked_non_evidence_command_still_nudges():
    """被拦的不是取证路径(如 ls) → 照常驳回。"""
    lg = _ledger()
    lg.note_tool_result("write_file", PY, WRITE_OK)
    lg.note_blocked("terminal", {"command": "ls -la"})
    assert lg.build_stop_nudge() is not None


def test_evidence_blocked_resets_per_turn():
    """"取证被阻断"只对当前轮有效:下一轮照常要求验证。"""
    lg = _ledger()
    lg.note_blocked("terminal", {"command": "python -m pytest tests/ -q"})
    lg.reset_for_turn()
    lg.note_tool_result("write_file", PY, WRITE_OK)
    assert lg.build_stop_nudge() is not None


# ============================================================
# 端到端(假 LLM 驱动真实循环,夹具见 conftest.py)
#
# 语义变更(2026-09-16):原"收尾验证门"是**判决者**(驳回/计次/上限),
# 现改为**情况说明**(陈述本轮改了什么、跑了什么,由模型自己判断)。
# 所以这里的断言从"驳回几次"改为"情况说明里有没有这些信息"。
# ============================================================
CTX_MARK = "[本轮运行情况]"

# 真跑一次 pytest 以便产生真实的执行记录,但只做 collect 以保持测试快速
_COLLECT = "python -m pytest tests/ -q --collect-only"


def _contexts(history):
    """本轮情况说明消息(合成 user 消息)。"""
    return [m for m in history
            if m.get("role") == "user" and CTX_MARK in str(m.get("content"))]


def test_e2e_context_lists_changed_file(run_script, tmp_path):
    """改完文件 → 情况说明里列出改动的文件(不判决、不驳回)。"""
    target = str(tmp_path / "app.py")
    agent, llm = run_script(
        [("write_file", {"path": target, "content": "x = 1\n", "verify": False})])

    ctxs = _contexts(agent.history)
    assert len(ctxs) == 1, "情况说明只给一次(给过就不再打扰)"
    assert "app.py" in ctxs[0]["content"], "应列出改动的文件"
    assert agent.history[-1]["role"] == "assistant", "最终以回答收尾"
    assert llm.main_calls == 3, "写入 + 想收工(附情况说明) + 最终收尾"


def test_e2e_context_lists_execution_records(run_script, tmp_path):
    """跑过命令 → 情况说明里带上执行记录与结果摘要。"""
    target = str(tmp_path / "app.py")
    agent, _ = run_script([
        ("write_file", {"path": target, "content": "x = 1\n", "verify": False}),
        ("terminal", {"command": _COLLECT}),
    ])

    ctx = _contexts(agent.history)[0]["content"]
    assert "pytest" in ctx, "应带上执行过的命令"
    assert "本轮执行记录" in ctx


def test_e2e_no_context_when_nothing_happened(run_script, tmp_path):
    """什么都没做 → 不附情况说明(不值得打扰)。"""
    agent, llm = run_script([])

    assert _contexts(agent.history) == []
    assert llm.main_calls == 1


def test_e2e_context_includes_raw_outcome_not_verdict(run_script, tmp_path):
    """情况说明应包含**原始结果**(含错误文本),而不是"通过/失败"的判决。

    这是新方向的要点:把事实摆出来让模型自己读。
    """
    missing = str(tmp_path / "nope.py")
    agent, _ = run_script([
        ("write_file", {"path": str(tmp_path / "app.py"),
                        "content": "x = 1\n", "verify": False}),
        ("read_file", {"path": missing}),
    ])

    ctx = _contexts(agent.history)[0]["content"]
    assert "不" in ctx or "error" in ctx.lower() or "错误" in ctx,         "原始结果(含错误信息)应原样出现在说明里"


def test_e2e_pairing_still_valid(run_script, tmp_path):
    """验证门不破坏 assistant(tool_calls) ↔ tool 回复的配对约束。"""
    from conftest import pairing_ok

    target = str(tmp_path / "app.py")
    agent, _ = run_script(
        [("write_file", {"path": target, "content": "x = 1\n", "verify": False})])

    ok, why = pairing_ok(agent.history)
    assert ok, why

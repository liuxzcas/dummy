"""turn_context 测试 —— 收尾情况说明(陈述事实 + 要求自查)。

============ 这个模块的定位 ============
从"收尾验证门(判决者)"退化为"信息提供者"之后,它只做两件事:
  1. 如实列出本轮改了什么、跑了什么(事实)
  2. **要求模型对照记录自查**(2026-09-16 加,防"生成假完成结论/假依据")

第 2 点是本模块唯一带"干预"性质的部分 —— 它不判决、不驳回,
只是把「你说的」和「记录里的」摆在一起,让模型自己核对。
"""
from turn_context import TurnContext, RunRecord


def _tc(**kw):
    return TurnContext(**kw)


# ============================================================
# 基本行为:记录事实
# ============================================================
def test_empty_returns_none():
    """什么都没做 → 不打扰(返回 None)。"""
    assert _tc().build_stop_context() is None


def test_lists_changed_files():
    tc = _tc()
    tc.note_tool_result("write_file", {"path": "a.py"}, "写入成功: a.py")
    ctx = tc.build_stop_context()
    assert "本轮改动/新建的文件" in ctx
    assert "a.py" in ctx


def test_dedupes_repeated_changes():
    """同一个文件多次写入 → **改动列表里只列一次**。

    注:执行记录里仍会各出现一次(那是流水账,如实记录每次执行),
    去重只针对"本轮改动/新建的文件"这个汇总列表。
    """
    tc = _tc()
    for i in range(3):
        tc.note_tool_result("write_file", {"path": "a.py"}, f"写入成功 ({i})")
    ctx = tc.build_stop_context()
    changes = ctx.split("本轮执行记录")[0]        # 只取改动列表那一节
    assert changes.count("a.py") == 1, f"改动列表应去重: {changes}"


def test_lists_execution_records():
    tc = _tc()
    tc.note_tool_result("terminal", {"command": "ls -la"}, "[SHELL: git-bash] total 41 ...")
    ctx = tc.build_stop_context()
    assert "本轮执行记录" in ctx
    assert "ls -la" in ctx


def test_records_are_capped():
    """执行记录有上限,避免撑爆上下文。"""
    tc = _tc()
    for i in range(30):
        tc.note_tool_result("terminal", {"command": f"cmd{i}"}, "ok")
    ctx = tc.build_stop_context()
    assert "cmd29" in ctx          # 最近的保留
    assert "cmd0\n" not in ctx     # 最早的被丢弃


def test_reset_clears_turn():
    tc = _tc()
    tc.note_tool_result("write_file", {"path": "a.py"}, "ok")
    tc.reset_for_turn()
    assert tc.build_stop_context() is None


def test_disabled_returns_none():
    tc = _tc(enabled=False)
    tc.note_tool_result("write_file", {"path": "a.py"}, "ok")
    assert tc.build_stop_context() is None


# ============================================================
# 关键:要求自查(防假完成 / 假依据)
# ============================================================
def test_asks_model_to_self_check():
    """必须明确要求模型对照记录自查 —— 这是防幻觉的关键机制。

    背景:收尾验证门(硬规则)被删掉后,"任务完成了吗"完全由模型自己判断。
    风险是它会顺着语义惯性写出没做过的动作(如"测试全部通过"但根本没跑)。
    本要求是唯一的应对:把记录摆出来,要它自己核对。
    """
    tc = _tc()
    tc.note_tool_result("write_file", {"path": "a.py"}, "写入成功")
    ctx = tc.build_stop_context()
    assert "请对照它检查你刚才的回答" in ctx


def test_asks_to_admit_unverified():
    """必须明确给出"没做到就说没做到"的选项。"""
    tc = _tc()
    tc.note_tool_result("write_file", {"path": "a.py"}, "写入成功")
    ctx = tc.build_stop_context()
    assert "没做的事就说没做" in ctx
    assert "未验证" in ctx, "应给出「未验证」这个说法,而不是只说「已通过」"
    assert "不确定" in ctx


def test_allows_late_verification():
    """给模型一个补救出口:发现该验没验,现在补也来得及。"""
    tc = _tc()
    tc.note_tool_result("write_file", {"path": "a.py"}, "写入成功")
    ctx = tc.build_stop_context()
    assert "补验" in ctx


def test_does_not_issue_verdicts():
    """文案里不能出现判决措辞 —— 它是信息,不是判决。"""
    tc = _tc()
    tc.note_tool_result("write_file", {"path": "a.py"}, "写入成功")
    ctx = tc.build_stop_context()
    for banned in ("驳回", "必须通过", "不允许收工", "判定为"):
        assert banned not in ctx, f"情况说明不该含判决措辞: {banned}"


# ============================================================
# RunRecord 渲染
# ============================================================
def test_run_record_renders_summary_and_outcome():
    r = RunRecord(tool="terminal", summary="ls -la", outcome="[SHELL] total 41")
    out = r.render()
    assert "ls -la" in out
    assert "total 41" in out


def test_long_output_is_digested():
    """超长输出要压成一行,不能把原始输出整段塞进去。"""
    tc = _tc()
    tc.note_tool_result("terminal", {"command": "big"}, "x" * 5000)
    ctx = tc.build_stop_context()
    assert len(ctx) < 2000, f"情况说明不该被单条输出撑爆(实际 {len(ctx)} 字符)"

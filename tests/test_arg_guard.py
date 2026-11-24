"""
tests/test_arg_guard.py — arg_guard:参数截断的证据保全

覆盖:
1. looks_truncated 结构判据:截断串为真;完整 JSON 为假
2. 正常 write_file 调用**不得**被误判(误报会打断每一次正常写入)
3. partial 文件落盘、字段完整、路径在 garage 下
4. 白名单:非 write_file 的工具不产生 partial、不返回提示(为将来推广留的边界)
5. 提示文案不含判决性措辞(框架不替模型决定续写还是重写)
6. 真实日志里的那条截断串能被识别(回归样本)
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import arg_guard  # noqa: E402


pytestmark = pytest.mark.fast   # 测试分组见 pytest.ini


@pytest.fixture()
def garage(tmp_path, monkeypatch):
    monkeypatch.setattr(arg_guard.workspace, "ensure", lambda: str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------
# 结构判据
# ---------------------------------------------------------------
def test_truncated_strings_detected():
    cases = [
        '{"content": "abc',                       # 引号未闭合
        '{"content": "abc"',                      # 缺右花括号
        '{"path": "a.py", "content": "def f():',  # 结尾无 }
        '{"a": [1, 2',                            # 数组未闭合
    ]
    for raw in cases:
        assert arg_guard.looks_truncated(raw) is True, raw


def test_quote_imbalance_alone_detected():
    """只靠"引号奇数"这一条就能判出来 —— 括号平衡、结尾也是 }，唯一缺陷是引号。"""
    raw = '{"path": "a.py", "content": "he said hi}'   # 引号 4 个?不,是 3 个奇数
    assert raw.count('"') % 2 == 1, raw.count('"')
    assert raw.count("{") == 1 and raw.count("}") == 1      # 括号平衡
    assert raw.rstrip()[-1] == "}"                          # 结尾正常
    assert arg_guard.looks_truncated(raw) is True


def test_bracket_alone_detected():
    """只靠"括号不平衡" —— 引号成对、结尾是 }，但少了一个右括号。"""
    raw = '{"a": {"b": 1}'              # { ×2, } ×1 → 不平衡;结尾是 }
    assert raw.count('"') % 2 == 0, raw.count('"')
    assert raw.rstrip()[-1] == "}"
    assert raw.count("{") != raw.count("}"), (raw.count("{"), raw.count("}"))
    assert arg_guard.looks_truncated(raw) is True


def test_tail_is_last_resort_only():
    """"结尾判据"只是兜底:引号和括号都平衡、但末尾不是闭合符的串。

    现实中这种形态罕见(截断几乎必然破坏引号或括号),所以它只在
    count 判据全部通过时才生效。这里用一条能被它单独命中的串验证它没被删掉。
    """
    raw = '["a", "b"]x'                 # 引号成对、方括号平衡,但结尾是 x
    assert raw.count('"') % 2 == 0
    assert raw.count("[") == raw.count("]")
    assert raw.rstrip()[-1] not in ("}", "]")
    assert arg_guard.looks_truncated(raw) is True


def test_complete_json_not_flagged():
    """正常参数绝不能被判成截断 —— 否则每次写入都会被拦。"""
    cases = [
        '{"path": "a.py", "content": "print(1)\\n"}',
        '{"content": "x", "mode": "append", "path": "D:/a.py"}',
        '{"path": "中文.py", "content": "def f():\\n    return 1\\n"}',
        json.dumps({"path": "a.py", "content": "多行\n内容\t带制表符" * 50}, ensure_ascii=False),
    ]
    for raw in cases:
        assert arg_guard.looks_truncated(raw) is False, raw


def test_empty_raw_not_flagged():
    assert arg_guard.looks_truncated("") is False


# ---------------------------------------------------------------
# 保全与提示
# ---------------------------------------------------------------
def test_partial_saved_with_evidence(garage):
    raw = '{"content": "def main():\\n    print(1'   # 截断
    note = arg_guard.guard("write_file", raw, {"content": "def main():\n    print(1"}, ["path"])
    assert note and "参数被截断" in note
    files = [f for f in os.listdir(garage / "_partials") if f.endswith(".json")]
    assert len(files) == 1, files
    data = json.loads((garage / "_partials" / files[0]).read_text(encoding="utf-8"))
    assert data["tool"] == "write_file"
    assert data["raw_length"] == len(raw)
    assert data["truncated"] is True
    assert data["raw_head"].startswith('{"content"')
    assert data["raw_tail"].endswith("print(1")
    assert data["missing_keys"] == ["path"]
    assert "未写入" in data["note"]


def test_partial_path_is_under_garage(garage):
    note = arg_guard.guard("write_file", '{"content": "x', {}, ["path"])
    assert "garage" in note or "_partials" in note
    assert (garage / "_partials").is_dir()


def test_note_has_no_verdict_wording(garage):
    """框架只陈述事实，不替模型决定怎么办。"""
    note = arg_guard.guard("write_file", '{"content": "x', {}, ["path"]) or ""
    for forbidden in ("你应该", "你必须", "请务必", "建议你"):
        assert forbidden not in note, forbidden


def test_note_does_not_say_continue_writing(garage):
    """禁止"继续/接着写"这类措辞。

    截断时**目标文件根本没写盘**,不存在"接着上次继续"这回事;
    说"追加/继续"会让模型以为用 append 就能接上,而 append 在文件不存在时
    会新建文件、前半段直接丢失(write_file.py:243)。
    """
    note = arg_guard.guard("write_file", '{"content": "x', {}, ["path"]) or ""
    for forbidden in ("继续写入", "接着写", "继续补充", "从断点继续"):
        assert forbidden not in note, forbidden


def test_note_warns_append_needs_existing_file(garage):
    """必须写出 append 的前置条件 —— 这是最容易踩的坑。"""
    note = arg_guard.guard("write_file", '{"content": "x', {}, ["path"]) or ""
    assert "append" in note
    assert "文件不存在时会新建" in note


def test_note_offers_both_paths(garage):
    """两条路径都要给:重写(推荐)与分段。"""
    note = arg_guard.guard("write_file", '{"content": "x', {}, ["path"]) or ""
    assert 'mode="overwrite"' in note
    assert 'mode="append"' in note
    assert "推荐" in note


# ---------------------------------------------------------------
# 连续截断提示
# ---------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_streak():
    """每个用例都从干净计数开始 —— 模块级全局会被用例间互相污染。"""
    arg_guard.reset_streak()
    yield
    arg_guard.reset_streak()


def test_first_truncation_has_no_repeat_hint(garage):
    """第 1 次截断不该出现"连续第 N 次"——那是给重复失败用的。"""
    note = arg_guard.guard("write_file", '{"content": "x', {}, ["path"]) or ""
    assert "连续第" not in note


def test_second_truncation_adds_repeat_hint(garage):
    """连续第 2 次 → 必须给出"换表达方式"的建议(实测 6 次同内容失败的教训)。"""
    arg_guard.guard("write_file", '{"content": "x', {}, ["path"])
    note = arg_guard.guard("write_file", '{"content": "y', {}, ["path"]) or ""
    assert "连续第 2 次" in note
    assert "三引号" in note          # 给出具体的替代做法,而不是空话
    assert "16724" in note           # 用实测数据说明"不是长度问题"


def test_streak_resets_after_success(garage):
    """成功执行后计数清零 → 下一次截断重新算第 1 次。"""
    arg_guard.guard("write_file", '{"content": "x', {}, ["path"])
    arg_guard.guard("write_file", '{"content": "y', {}, ["path"])
    arg_guard.reset_streak()
    note = arg_guard.guard("write_file", '{"content": "z', {}, ["path"]) or ""
    assert "连续第" not in note


def test_streak_counts_up(garage):
    """连续多次 → 数字递增,不会卡在 2。"""
    for _ in range(3):
        note = arg_guard.guard("write_file", '{"content": "x', {}, ["path"]) or ""
    assert "连续第 3 次" in note


def test_other_tools_not_saved(garage):
    """白名单:terminal 等工具不产生 partial(当前边界)。"""
    raw = '{"command": "echo hi'          # 截断,但不在白名单
    assert arg_guard.guard("terminal", raw, {"command": "echo hi"}) is None
    assert not (garage / "_partials").exists()


def test_non_write_tool_whitelist_is_explicit():
    """推广边界写死:将来加工具必须显式改白名单。"""
    assert arg_guard.TOOLS_WITH_PARTIAL == frozenset({"write_file"})


def test_prune_keeps_bounded(garage, monkeypatch):
    """上限必须真的生效:先确认 6 次调用落 6 个文件(不被覆盖),再看裁剪到 3。"""
    monkeypatch.setattr(arg_guard, "MAX_PARTIALS", 3)
    created = [
        arg_guard.guard("write_file", '{"content": "x' + "a" * i, {}, ["path"])
        for i in range(6)
    ]
    files = [f for f in os.listdir(garage / "_partials") if f.endswith(".json")]
    assert len(files) == 3, f"应裁剪到 3, 实际 {len(files)}: {files}"
    # 每次调用都应拿到不同的路径(证明文件名唯一,没互相覆盖)
    paths = [p for p in created if p]
    assert len(set(paths)) == len(paths), paths


# ---------------------------------------------------------------
# 真实回归样本
# ---------------------------------------------------------------
def test_real_truncated_call_from_logs(garage):
    """logs/conversation_20260814_001407.json #62 的真实截断串。"""
    raw = ('{"content": "# -*- coding: utf-8 -*-\\n\\"\\"\\"\\n多策略对比分析\\n'
           + "x" * 500 + '\\n    ax.legend(loc=\'lower left\', fontsize=10)\\n    ax.grid(')
    assert arg_guard.looks_truncated(raw) is True
    note = arg_guard.guard("write_file", raw, {"content": "m"}, ["path"]) or ""
    assert "参数被截断" in note and "ax.grid(" in note.replace("\\n", "\n")

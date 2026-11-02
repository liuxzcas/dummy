"""参数错误提示 —— 必须可操作(2026-10-30 用户实测驱动)。

============ 背景 ============
用户:"dummy 的总是会出现模型传参传错,write_file 调用失败"
      "新用户第一次部署容易出错;复杂长任务多轮后概率也升高"

实测统计(742 次 write_file 调用,36 次失败 = 4.8%):
    其他错误(主要是 line=0)  16 次
    缺 path 参数             14 次
    用户拒绝                  6 次

两类真问题的根因:

**① required 把所有参数都标成必填**
    registry 里 `"required": list(parameters.keys())` ——
    write_file 有 6 个参数,其中 4 个有默认值,却全被标必填。
    → 模型以为 line 必填,只好瞎填 0 → "行号越界"16 次。

**② 错误信息不可操作**
    缺 path 时报的是 Python 内部错误:
        TypeError: write_file_handler() missing 1 required
        positional argument: 'path'
    → 没说明该怎么改;实测同一会话里同类错误连犯两次(第 86/92 条)。

============ 本文件锁住什么 ============
  · schema 的 required 只含真正必填的参数
  · 缺参数时的提示要含:缺什么 / 已收到什么 / 该给什么 / 示例 / 下一步
  · line<1、line 越界、mode=line 缺 line,各自给出针对性说明
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import create_default_registry   # noqa: E402


@pytest.fixture
def reg():
    r = create_default_registry()
    r.set_confirm_provider(lambda p: "y")
    return r


@pytest.fixture
def tmpfile(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    return str(f)


# ============================================================
# ① schema 的 required 必须准确
# ============================================================
class TestRequiredAccuracy:
    """required 只含真正必填 —— 可选参数不该被标必填。"""

    def _required(self, reg, name):
        for d in reg.get_tool_definitions():
            if d["function"]["name"] == name:
                return d["function"]["parameters"]["required"]
        raise AssertionError(f"没找到工具 {name}")

    def test_write_file_only_path_and_content(self, reg):
        """write_file 有 6 个参数,但只有 path/content 是真必填。

        实测后果:mode/line/line_end/verify 被误标必填 →
        模型以为 line 必须给,填了 0 → "行号越界"16 次。
        """
        assert self._required(reg, "write_file") == ["path", "content"]

    def test_other_tools(self, reg):
        assert self._required(reg, "terminal") == ["command"]
        assert self._required(reg, "read_file") == ["path"]
        assert self._required(reg, "web_search") == ["query"]
        assert self._required(reg, "web_extract") == ["url"]

    def test_optional_params_still_in_properties(self, reg):
        """可选参数仍要出现在 properties 里(只是不标 required)。"""
        for d in reg.get_tool_definitions():
            if d["function"]["name"] == "write_file":
                props = d["function"]["parameters"]["properties"]
                assert set(props) == {"path", "content", "mode",
                                      "line", "line_end"}
                break


# ============================================================
# ② 缺参数 → 可操作的提示
# ============================================================
class TestMissingParamMessage:
    def test_missing_path_mentions_everything_needed(self, reg):
        """提示必须含:缺什么、已收到、该给什么、示例、下一步。"""
        out = reg.dispatch("write_file", {"content": "print('hi')"})

        assert "缺少必填参数" in out and "path" in out, out
        assert "已收到" in out and "content" in out, f"要说明收到了什么: {out}"
        assert "该工具需要" in out, f"要列出全部必填: {out}"
        assert "正确示例" in out, f"要给可照抄的示例: {out}"
        assert "重新调用" in out, f"要说明下一步: {out}"

    def test_not_a_raw_python_typeerror(self, reg):
        """不该再是 Python 的 TypeError 原文。

        旧消息:TypeError: write_file_handler() missing 1 required
                positional argument: 'path'
        """
        out = reg.dispatch("write_file", {"content": "x"})
        assert "TypeError" not in out
        assert "positional argument" not in out

    def test_shows_received_arg_sizes(self, reg):
        """已收到的参数要带规模 —— 帮模型确认它想传的内容在不在。"""
        out = reg.dispatch("write_file", {"content": "x" * 1234})
        assert "1234" in out, f"应报出 content 的长度: {out}"

    def test_empty_string_counts_as_missing(self, reg):
        """空字符串等于没给。"""
        out = reg.dispatch("write_file", {"path": "", "content": "x"})
        assert "缺少必填参数" in out and "path" in out

    def test_all_missing(self, reg):
        out = reg.dispatch("write_file", {})
        assert "path" in out and "content" in out
        assert "(无)" in out or "已收到" in out

    def test_internal_params_not_reported(self, reg):
        """内部注入的 _confirm 不该出现在"已收到"里。"""
        out = reg.dispatch("write_file", {"content": "x"})
        assert "_confirm" not in out

    def test_other_tools_also_helpful(self, reg):
        """不只 write_file —— 所有工具缺参数都要给同样的提示质量。"""
        out = reg.dispatch("terminal", {})
        assert "缺少必填参数" in out and "command" in out
        assert "正确示例" in out


# ============================================================
# ③ line 相关错误要说清原因
# ============================================================
class TestLineErrors:
    def test_line_zero_explains_one_based(self, reg, tmpfile):
        """line=0 要说明"从 1 开始",并给替代方案。

        实测 16 次 —— 模型按 0-based 思维填的,
        只说"越界"它不知道原因(同一会话连犯两次)。
        """
        out = reg.dispatch("write_file", {
            "path": tmpfile, "content": "x", "mode": "line", "line": 0})
        assert "从 1 开始" in out, f"要说明计数起点: {out}"
        assert "收到 0" in out
        assert "overwrite" in out, f"要给出替代方案: {out}"

    def test_line_out_of_range_shows_valid_range(self, reg, tmpfile):
        out = reg.dispatch("write_file", {
            "path": tmpfile, "content": "x", "mode": "line", "line": 99})
        assert "超出范围" in out
        assert "1~3" in out or "共 3 行" in out, f"要给出有效范围: {out}"

    def test_mode_line_without_line(self, reg, tmpfile):
        out = reg.dispatch("write_file", {
            "path": tmpfile, "content": "x", "mode": "line"})
        assert "必须指定 line" in out
        assert "overwrite" in out

    def test_line_end_out_of_range(self, reg, tmpfile):
        out = reg.dispatch("write_file", {
            "path": tmpfile, "content": "x", "mode": "line",
            "line": 2, "line_end": 99})
        assert "line_end" in out and "超出范围" in out

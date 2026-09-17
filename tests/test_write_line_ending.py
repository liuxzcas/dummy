"""write_file 的换行符回报测试。

============ 为什么要这个功能 ============
2026-09-17 实测(conversation_20260917_175951):模型写完 .bat 后,又额外
跑了一次 `sed -i 's/\r$//; s/$/\r/'` 去"修"换行符 —— 而那次调用**完全是
多余的**:Python 在 Windows 上 open(...,"w") 本来就做平台转换,写出来
已经是 CRLF。

根因不是能力缺失(它写对了),是**信息缺失**:模型看不到写出来的是什么换行。
所以修法是"如实报告",不是"加参数"——报告不占工具描述(0 常驻 token)。
"""
import os

from tools.write_file import _detect_line_ending, write_file_handler


# ============================================================
# 探测函数:必须报事实
# ============================================================
def test_detects_crlf(tmp_path):
    p = tmp_path / "a.bat"
    p.write_bytes(b"a\r\nb\r\n")
    assert _detect_line_ending(str(p)) == "CRLF"


def test_detects_lf(tmp_path):
    p = tmp_path / "a.sh"
    p.write_bytes(b"a\nb\n")
    assert _detect_line_ending(str(p)) == "LF"


def test_detects_mixed(tmp_path):
    """混合换行必须能识别 —— line 模式改单行时最容易产生。

    如果只报"CRLF"会掩盖问题(文件里其实有裸 LF 行)。
    """
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\r\nb\nc\r\n")
    result = _detect_line_ending(str(p))
    assert "混合" in result
    assert "CRLF" in result and "LF" in result


def test_no_newline(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"no newline at all")
    assert _detect_line_ending(str(p)) == "无换行"


def test_empty_file(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"")
    assert _detect_line_ending(str(p)) == "无换行"


def test_missing_file_returns_placeholder():
    """文件不存在时返回占位符而不是抛异常。

    探测是"回报用"的,绝不能因为它失败而让写入流程报错。
    """
    assert _detect_line_ending("_definitely_not_here_") == "?"


# ============================================================
# 端到端:返回值里必须带换行符
# ============================================================
def test_write_result_reports_line_ending(tmp_path, monkeypatch):
    """写入成功的返回值里必须有换行符信息 —— 这是让模型不再多跑 sed 的关键。"""
    monkeypatch.chdir(tmp_path)
    result = write_file_handler(path="x.bat", content="@echo off\necho hi\n",
                                verify=False)
    assert "写入成功" in result
    assert "[换行" in result, f"返回值应报告换行符,实际: {result!r}"


def test_reported_ending_matches_disk(tmp_path, monkeypatch):
    """报告的换行符必须与磁盘实际内容一致(读出来判断,不是假设)。"""
    monkeypatch.chdir(tmp_path)
    result = write_file_handler(path="x.txt", content="a\nb\n", verify=False)
    raw = (tmp_path / "x.txt").read_bytes()
    if b"\r\n" in raw:
        assert "CRLF" in result
    elif b"\n" in raw:
        assert "LF" in result
    else:
        assert "无换行" in result


def test_report_matches_actual_not_assumption(tmp_path, monkeypatch):
    """**关键契约**:报告的是事实,不是"Python 应该会转换"的假设。

    做法:手写一个 LF 文件,再验证探测返回 LF —— 即使当前平台是 Windows
    (在 Windows 上 Python 默认会写 CRLF,但探测不看平台,只看文件)。
    """
    p = tmp_path / "forced_lf.txt"
    p.write_bytes(b"a\nb\n")          # 手工写 LF,绕过 write_file
    assert _detect_line_ending(str(p)) == "LF", \
        "探测必须读文件实际内容,不能按平台假设"

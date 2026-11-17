"""控制序列过滤 —— 修"清屏"（2026-11-17 定位到真凶）。

============ 真凶 ============
用户从 2026-11-05 起反复报"工具轮间每隔一会儿就清屏,之前内容滚不到"。
排查了 7 个隔离实验(纯 print / 带颜色码 / 反复 prompt / 逐轮加量 /
单条超长 / 超长中文 / 5 次重复)全部**复现不了**。

最后靠 stdout 探针 + 数据库扫描抓到铁证:
    messages 表 332 条 tool 消息,尾部全是同一串
        ...\\n\\x1b[H\\x1b[2J\\x1b[3J
    \\x1b[2J 清屏  \\x1b[3J 连 scrollback 一起清  \\x1b[H 光标归位

于是每次把这些内容打印出来(工具结果回显、/history 日志回放)
终端就真的执行了清屏 —— 这就是"滚一点点就到顶"的机械原因。
最早出现时间 2026-11-05,与用户首次报障同一天。

============ 三道防线 ============
① tools/terminal.py 入口就剥(一处收口,新数据从此干净)
② ui.raw / ui.emit 打印前再剥(纵深防御,挡住 db 里已有的旧数据)
③ core.resume_session 载入历史时剥(存量污染在入口消解)

任何一道单独都能挡住;三道都在,是因为"终端被清屏"的代价
(用户丢失全部可见历史)远高于多剥一次的代价。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 真凶原样
CLEAR_SEQ = "\x1b[H\x1b[2J\x1b[3J"


# ============================================================
# ① terminal 工具入口
# ============================================================
class TestTerminalStripsAnsi:
    def test_strip_ansi_removes_clear_sequence(self):
        from tools.terminal import strip_ansi
        assert strip_ansi("ok\n" + CLEAR_SEQ) == "ok\n"

    def test_strip_ansi_removes_colors(self):
        from tools.terminal import strip_ansi
        assert strip_ansi("\x1b[33m警告\x1b[0m: 3 个问题") == "警告: 3 个问题"

    def test_strip_ansi_keeps_plain_text(self):
        from tools.terminal import strip_ansi
        s = "正常输出\n第二行\r\n第三行"
        assert strip_ansi(s) == s        # 无 ESC 时原样返回

    def test_strip_ansi_removes_cursor_moves(self):
        from tools.terminal import strip_ansi
        assert strip_ansi("a\x1b[2Ab\x1b[10;20Hc") == "abc"

    def test_decode_and_clean(self):
        from tools.terminal import _decode_and_clean
        raw = b"ok\n" + CLEAR_SEQ.encode()
        assert _decode_and_clean(raw) == "ok\n"

    def test_decode_and_clean_handles_none(self):
        from tools.terminal import _decode_and_clean
        assert _decode_and_clean(None) == ""

    def test_real_terminal_output_has_no_esc(self):
        """端到端:跑真命令,返回里不能有 ESC。"""
        from tools import create_default_registry
        reg = create_default_registry()
        reg.set_confirm_provider(lambda p: "")
        out = reg.dispatch("terminal", {"command": "echo ok"})
        assert "\x1b" not in out, f"输出里还有 ESC: {out!r}"


# ============================================================
# ② 打印层(纵深防御)
# ============================================================
class TestUiStripsControl:
    def test_strip_control_removes_clear_sequence(self):
        import ui as ui_mod
        assert ui_mod._strip_control("ok\n" + CLEAR_SEQ) == "ok\n"

    def test_raw_output_clean(self, capsys):
        """ui.raw 打印时不能把清屏序列放出去。"""
        import ui as ui_mod
        ui_mod.set_active_app(None)
        ui_mod.ui.raw("正文" + CLEAR_SEQ)
        out = capsys.readouterr().out
        assert "\x1b" not in out, f"raw 把控制序列放出去了: {out!r}"
        assert "正文" in out

    def test_emit_output_clean(self, capsys, monkeypatch):
        import ui as ui_mod
        monkeypatch.setattr(ui_mod, "ui", ui_mod.UI(ui_mod.PlainRenderer()))
        ui_mod.set_active_app(None)
        ui_mod.ui.show(ui_mod.KIND_AGENT, "回复" + CLEAR_SEQ)
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "回复" in out

    def test_plain_text_untouched(self, capsys):
        """没有 ESC 的文本必须原样输出(不能误伤)。"""
        import ui as ui_mod
        ui_mod.set_active_app(None)
        ui_mod.ui.raw("正常的一行,含 [方括号] 和 % 百分号")
        out = capsys.readouterr().out
        assert "[方括号]" in out and "%" in out


# ============================================================
# ③ 恢复历史时清洗(存量污染)
# ============================================================
class TestResumeCleans:
    """存量污染:db 里 2026-11-05 之后的 332 条消息都带着清屏序列。

    ★ 第一版测试是假的:mock 掉 store 后 resume_session 在
      acquire_session_lock 处提前返回,history 根本没赋值,
      断言空过 —— 回滚代码也照样绿。所以改成直接测抽出来的
      _clean_history_controls(),它是真正承载逻辑的那一段。
    """

    def test_cleans_dirty_history(self):
        import core
        dirty = [
            {"role": "system", "content": "system"},
            {"role": "tool", "content": "ok\n" + CLEAR_SEQ},
            {"role": "tool", "content": "干净的内容"},
        ]
        n = core._clean_history_controls(dirty)
        assert n == 1, f"应清理 1 条,实际 {n}"
        assert dirty[1]["content"] == "ok\n"
        assert dirty[2]["content"] == "干净的内容", "干净的不该被改动"

    def test_handles_non_string_content(self):
        """多模态 content 是 list,不能崩。"""
        import core
        hist = [{"role": "tool", "content": [{"type": "text", "text": "x"}]}]
        assert core._clean_history_controls(hist) == 0

    def test_idempotent(self):
        import core
        hist = [{"role": "tool", "content": "ok\n" + CLEAR_SEQ}]
        assert core._clean_history_controls(hist) == 1
        assert core._clean_history_controls(hist) == 0   # 第二次没得清

    def test_resume_calls_it(self):
        """接线检查:resume_session 里必须真的调用(防"抽出来没人用")。"""
        import ast
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "core.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "resume_session"):
                assert "_clean_history_controls" in ast.unparse(node), \
                    "resume_session 没调用清洗函数 —— 旧会话恢复时仍会清屏"
                return
        raise AssertionError("没找到 resume_session")

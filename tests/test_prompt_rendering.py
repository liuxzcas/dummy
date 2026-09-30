"""输入提示符着色 —— readline 适配的测试。

============ 背景（2026-09-30，用户在 Ubuntu 上实测发现） ============
dummy 的交互提示符里输入中文会出现：
  · 退格只擦掉半个字，留残影
  · 方向键无法移动光标（回显字面量 ^[[D）
  · 严重时 input() 抛 UnicodeDecodeError

根因：**全项目没有 import readline** → input() 退回 tty canonical 模式
（按"字节/列"处理，不懂 UTF-8 与宽字符）。bash 自带 readline，
所以终端里正常、而本程序的提示符不正常。

修法两半：
  ① import readline        —— 启用行编辑（main.py）
  ② \001/\002 包裹 ANSI 码 —— 让 readline 不计提示符宽度（colors.paint_prompt）

============ 本文件锁住的关键契约 ============
② 有个平台陷阱：**没有 readline 时，\001/\002 会原样漏出去变成乱码**。
实测（Windows 无 readline）：
    b'\x01\x1b[92m\x02你 > \x01\x1b[0m\x02'
    终端显示: ^A^[[92m^B你 > ^A^[[0m^B

所以 paint_prompt 必须按 _HAS_READLINE 分支：
    有 readline → 加包裹（Linux / Windows+pyreadline3）
    无 readline → 不加（干净输出，且与旧版 paint() 字节一致）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import colors   # noqa: E402


@pytest.fixture
def colored(monkeypatch):
    """强制开启着色(测试环境 stdout 不是 TTY,默认是无色的)。"""
    monkeypatch.setattr(colors, "_ENABLED", True)
    return colors


# ============================================================
# 有 readline：加 \001/\002 包裹
# ============================================================
def test_prompt_wraps_ansi_when_readline_available(colored, monkeypatch):
    """有 readline 时,ANSI 码必须被 \\001/\\002 包住。

    这是修中文退格/光标错位的核心 —— readline 见到这两个字节就跳过,
    不再把转义序列算进提示符宽度。
    """
    monkeypatch.setattr(colored, "_HAS_READLINE", True)
    p = colored.paint_prompt("你 > ", colored.GREEN)
    assert "\x01" in p, "缺少 \\001(SOH)"
    assert "\x02" in p, "缺少 \\002(STX)"
    # 包裹必须紧贴转义序列
    assert "\x01" + colored.GREEN + "\x02" in p
    assert "\x01" + colored._RESET + "\x02" in p


def test_prompt_contains_prefix_and_color(colored, monkeypatch):
    monkeypatch.setattr(colored, "_HAS_READLINE", True)
    p = colored.paint_prompt("你 > ", colored.GREEN)
    assert "你 > " in p
    assert colored.GREEN in p


# ============================================================
# 无 readline：不加包裹（避免漏出控制字符）
# ============================================================
def test_prompt_has_no_markers_without_readline(colored, monkeypatch):
    """没有 readline 时**不能**加 \\001/\\002 —— 它们会原样漏出去变成乱码。

    实测:Windows 无 readline 时终端显示 ^A^[[92m^B你 > ^A^[[0m^B
    """
    monkeypatch.setattr(colored, "_HAS_READLINE", False)
    p = colored.paint_prompt("你 > ", colored.GREEN)
    assert "\x01" not in p, "不该有 \\001(会变成 ^A)"
    assert "\x02" not in p, "不该有 \\002(会变成 ^B)"
    assert "你 > " in p
    assert colored.GREEN in p          # 颜色还是要的


def test_prompt_without_readline_matches_plain_paint(colored, monkeypatch):
    """无 readline 时,paint_prompt 的输出必须与旧版 paint() **字节相同**。

    这条锁住"对 Windows(无 pyreadline3)零影响" —— 那些用户的行为
    与改动前完全一致。
    """
    monkeypatch.setattr(colored, "_HAS_READLINE", False)
    assert colored.paint_prompt("你 > ", colored.GREEN) == colored.paint("你 > ", colored.GREEN)


# ============================================================
# 无色模式
# ============================================================
def test_prompt_plain_when_color_disabled(monkeypatch):
    """DUMMY_COLOR 关闭时,提示符必须是纯文本(无 ANSI、无标记)。"""
    monkeypatch.setattr(colors, "_ENABLED", False)
    monkeypatch.setattr(colors, "_HAS_READLINE", True)
    assert colors.paint_prompt("你 > ", colors.GREEN) == "你 > "


# ============================================================
# readline 探测本身
# ============================================================
def test_detect_readline_returns_bool():
    """_detect_readline() 返回布尔值,不抛异常（任何平台）。"""
    assert isinstance(colors._detect_readline(), bool)


def test_has_readline_is_set_at_import():
    """_HAS_READLINE 在 import 期就确定（进程级事实）。"""
    assert isinstance(colors._HAS_READLINE, bool)


# ============================================================
# ui.py 接线
# ============================================================
def test_ui_prompt_uses_paint_prompt(monkeypatch):
    """ui.prompt() 必须走 paint_prompt（否则提示符宽度又算错）。

    注意:**不要 importlib.reload(ui)** —— 那会重新执行 ui.py 的
    模块级代码(它在 import 时创建全局实例 `ui = _make_ui()`),
    导致其他测试里引用的旧实例状态错乱(实测:reload 之后
    test_ui.py::test_renderer_selection_by_env 会失败)。
    这里改为直接调 UI.prompt(),不动模块。
    """
    monkeypatch.setattr(colors, "_ENABLED", True)
    monkeypatch.setattr(colors, "_HAS_READLINE", True)
    import ui as ui_mod
    u = ui_mod.UI(ui_mod.PlainRenderer())
    got = u.prompt()
    assert "\x01" in got, "ui.prompt() 没用 paint_prompt(缺少 \\001 标记)"
    assert "你 > " in got


def test_ui_prompt_has_no_marker_when_readline_absent(monkeypatch):
    """无 readline 时 ui.prompt() 也要干净(不带 \\001/\\002)。"""
    monkeypatch.setattr(colors, "_ENABLED", True)
    monkeypatch.setattr(colors, "_HAS_READLINE", False)
    import ui as ui_mod
    u = ui_mod.UI(ui_mod.PlainRenderer())
    got = u.prompt()
    assert "\x01" not in got
    assert "你 > " in got

"""ui.py 测试 —— 输出层收口 + 可插拔渲染。

============ 这个模块的两条核心契约 ============
① **拼与印分离**:Message 只有数据,渲染器决定怎么画。
   将来做 GUI 只换渲染器,业务代码不动。
② **raw 不被静音**(方案 B):对话流是"给人眼睛看的",测试时该静音;
   但 raw 是**程序 stdout**(命令输出、工具确认面板),重定向时必须保留,
   否则 `python main.py > log.txt` 会得到空文件。
"""
import io
import contextlib

import ui as ui_mod
from ui import (
    UI, Message, PlainRenderer, LineRenderer, SilentRenderer,
    KIND_USER, KIND_AGENT, KIND_TOOL_CALL, KIND_TOOL_RESULT,
    KIND_THINKING, KIND_SPINNER, KIND_NOTE, KIND_WARN, KIND_ERROR,
    KIND_DIVIDER, KIND_RAW,
)


def _capture(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


# ============================================================
# ① 拼与印分离
# ============================================================
def test_message_holds_only_data():
    """Message 是纯数据 —— 不含任何样式/颜色信息。

    这是"拼印分离"的根本:如果 Message 里塞了 ANSI 码或者 emoji,
    换渲染器时就得改业务代码,分层就失效了。
    """
    m = Message(kind=KIND_TOOL_CALL, tool="terminal", args={"command": "ls"})
    assert "\033[" not in str(m)          # 无 ANSI 码
    assert m.tool == "terminal"
    assert m.args == {"command": "ls"}


def test_build_then_emit():
    """build() 只拼不印;emit() 只印。"""
    u = UI(PlainRenderer())
    msg = u.build(KIND_AGENT, "你好")
    # build 不产生输出
    assert _capture(lambda: u.build(KIND_AGENT, "x")) == ""
    # emit 才输出
    assert "你好" in _capture(lambda: u.emit(msg))


def test_show_is_build_plus_emit():
    """show() = build + emit(最常用的便捷形态)。"""
    u = UI(PlainRenderer())
    out = _capture(lambda: u.show(KIND_AGENT, "内容"))
    assert "内容" in out


# ============================================================
# ② 渲染器可插拔
# ============================================================
def test_line_renderer_adds_bar_to_conversation_flow():
    u = UI(LineRenderer())
    out = _capture(lambda: u.show(KIND_TOOL_CALL, tool="terminal", args={}))
    assert out.startswith("┃")


def test_line_renderer_barres_every_line():
    """多行内容**每一行**都要有竖线 —— "框住"的关键。"""
    u = UI(LineRenderer())
    out = _capture(lambda: u.show(KIND_TOOL_RESULT, "第一行\n第二行\n第三行",
                                  tool="terminal", indent="  "))
    lines = [l for l in out.split("\n") if l.strip()]
    assert len(lines) == 3, f"应有三行: {out!r}"
    assert all(l.startswith("┃") for l in lines), f"每行都该有竖线: {out!r}"


def test_line_renderer_skips_bar_for_notes():
    """状态旁白(note/warn/error)**不加竖线** —— 它们不是对话流。

    边界见 ui.py 模块头:竖线只画对话流,交互面板与旁白保持原样。
    """
    u = UI(LineRenderer())
    for kind in (KIND_NOTE, KIND_WARN, KIND_ERROR):
        out = _capture(lambda k=kind: u.show(k, "旁白"))
        assert not out.startswith("┃"), f"{kind} 不该有竖线: {out!r}"


def test_plain_renderer_unchanged():
    """plain 是默认实现,视觉必须与收口前一致(保证可无痛回退)。"""
    u = UI(PlainRenderer())
    out = _capture(lambda: u.show(KIND_TOOL_CALL, tool="terminal", args={}))
    assert "🛠 Agent 调用了 [terminal]" in out
    assert not out.startswith("┃")


# ============================================================
# ③ raw:静音时必须保留(方案 B)
# ============================================================
def test_silent_keeps_raw_output():
    """silent 下 raw **必须照常输出** —— 这是方案 B 的核心契约。

    理由:raw 承载的是程序 stdout(/help、会话列表、工具确认面板),
    用户 `python main.py > log.txt` 时若被静音,文件会变空。
    对话流则相反 —— 那是给人看的,测试时静音才干净。
    """
    u = UI(SilentRenderer())
    out = _capture(lambda: u.raw("命令输出"))
    assert "命令输出" in out, "raw 在 silent 下必须保留"


def test_silent_drops_conversation_flow():
    """silent 下对话流必须静音。"""
    u = UI(SilentRenderer())
    for kind in (KIND_AGENT, KIND_TOOL_CALL, KIND_TOOL_RESULT,
                 KIND_THINKING, KIND_SPINNER):
        out = _capture(lambda k=kind: u.show(k, "噪音", tool="t"))
        assert out == "", f"{kind} 在 silent 下应静音,实际: {out!r}"


def test_raw_works_in_all_renderers():
    """raw 在三种渲染器下都输出(plain/line 原样,silent 也保留)。"""
    for r in (PlainRenderer(), LineRenderer(), SilentRenderer()):
        out = _capture(lambda rr=r: UI(rr).raw("X"))
        assert "X" in out, f"{type(r).__name__} 的 raw 没输出"


def test_raw_not_barred_in_line_mode():
    """line 模式下 raw 也不加竖线 —— 交互面板不属于对话流。"""
    u = UI(LineRenderer())
    out = _capture(lambda: u.raw("  即将执行: ls"))
    assert not out.startswith("┃")


# ============================================================
# 渲染器选择
# ============================================================
def test_renderer_selection_by_env(monkeypatch):
    monkeypatch.setenv("DUMMY_UI", "line")
    assert isinstance(ui_mod._make_ui().renderer, LineRenderer)
    monkeypatch.setenv("DUMMY_UI", "plain")
    assert isinstance(ui_mod._make_ui().renderer, PlainRenderer)
    monkeypatch.setenv("DUMMY_UI", "silent")
    assert isinstance(ui_mod._make_ui().renderer, SilentRenderer)


def test_prompt_returns_string():
    """prompt() 返回字符串(给 input() 用),不是打印。"""
    u = UI(PlainRenderer())
    out = _capture(lambda: u.prompt())
    assert out == "", "prompt 不该产生输出"
    assert isinstance(u.prompt(), str)

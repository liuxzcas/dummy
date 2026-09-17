"""ui.py — 面向用户的输出层(收口 + 可插拔渲染)。

============ 分层原则(用户 2026-09-16 定) ============
把两个动作**分开**:

  ① 拼 / 追加 —— 决定"这条消息是什么":语义、内容、属于哪一批
  ② 打印      —— 决定"怎么画":print / 竖线 / 发给 GUI

    业务代码 ──build()──> Message ──emit()──> 渲染器
               (拼:逻辑)              (印:表现)

为什么必须分开:**将来做 GUI 时,print() 根本不适用**。届时只换 ② 的实现,
① 完全不动。这是收口真正的价值 —— 不是为了现在的竖线,而是为了以后。

============ 可插拔渲染 ============
DUMMY_UI = plain(默认) | line | silent
    plain   现在的样式(图标 + 前缀着色)
    line    竖线 + 分隔线(**只作用于对话流**,见下)
    silent  什么都不打(测试 / GUI 接管时)

============ 竖线的边界(用户 2026-09-16 定) ============
竖线**仅限于"对话流"**:工具调用 / 工具结果 / 思考 / 回复。
**工具内部的确认面板**(read_file 的隐私提示、terminal 的确认、
write_file 的 diff)保持原样 —— 它们是"问用户话"的交互面板,
不是"叙述过程",视觉上分开反而更清楚。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from colors import (
    paint, GREEN, BLUE, SLATE, WHITE, GRAY, GRAY_DIM,
    PURPLE, YELLOW, RED, NEUTRAL, CYAN,
)


# ===============================================================
# ① 消息对象(拼的产物)
# ===============================================================
# kind 是**语义**(渲染器据此决定画法),不是样式。
# 加新语义只需加一个 kind + 在渲染器里处理,业务代码写 kind 即可。
KIND_USER = "user"          # 用户输入
KIND_AGENT = "agent"        # Agent 回复
KIND_THINKING = "thinking"  # 思考内容
KIND_SPINNER = "spinner"    # 进行中提示
KIND_TOOL_CALL = "tool_call"
KIND_TOOL_RESULT = "tool_result"
KIND_NOTE = "note"          # 状态提示(注入/压缩/自愈等)
KIND_WARN = "warn"
KIND_ERROR = "error"
KIND_DIVIDER = "divider"
KIND_RAW = "raw"            # 原样打印(工具内部确认面板等,不经渲染改造)


@dataclass
class Message:
    """一条要显示的消息。**只有数据,没有表现方式。**"""

    kind: str
    text: str = ""
    # 工具类消息附加:工具名、参数、是否缩进(结果相对调用缩进一层)
    tool: str = ""
    args: Any = None
    indent: str = ""
    # 原样输出的内容(raw 用)
    raw: str = ""


# ===============================================================
# ② 渲染器(印的实现)
# ===============================================================
class PlainRenderer:
    """现在的样式:图标 + 前缀着色。默认实现(保证视觉零变化)。"""

    indent = "  "

    def render(self, msg: Message) -> str | None:
        """把 Message 变成要 print 的字符串;返回 None 表示不输出。"""
        k = msg.kind
        if k == KIND_USER:
            return f"{paint('你 > ', GREEN)}{msg.text}"
        if k == KIND_AGENT:
            return f"\n{paint('🤖 Agent:', WHITE)} {msg.text}\n"
        if k == KIND_THINKING:
            return f"\n{self.indent}{paint('💭 思考:', GRAY)} {msg.text}"
        if k == KIND_SPINNER:
            return f"{self.indent}{paint(f'🤔 {msg.text}', GRAY_DIM)}"
        if k == KIND_TOOL_CALL:
            return (f"\n{self.indent}{paint('🛠 Agent 调用了', BLUE)} "
                    f"[{msg.tool}] 参数={msg.args}")
        if k == KIND_TOOL_RESULT:
            return f"{self.indent}{paint(f'📝 {msg.tool} 返回:', SLATE)} {msg.text}"
        if k == KIND_NOTE:
            return f"{self.indent}{msg.text}"
        if k == KIND_WARN:
            return f"{self.indent}{paint(f'⚠️ {msg.text}', YELLOW)}"
        if k == KIND_ERROR:
            return f"{self.indent}{paint(f'❌ {msg.text}', RED)}"
        if k == KIND_DIVIDER:
            # plain 不画线,也不留空行(收口前这里本来就没有输出)
            return None
        if k == KIND_RAW:
            return msg.raw
        return msg.text


class LineRenderer(PlainRenderer):
    """竖线样式:**只作用于对话流**(tool_call / tool_result / thinking /
    spinner / agent)。

    其他 kind(note / warn / error)保持 PlainRenderer 的表现 —— 它们是
    "状态旁白",不是对话内容,不加竖线反而清爽。
    """

    BAR = "┃"
    # 需要竖线的 kind(对话流)
    BARRED = {KIND_TOOL_CALL, KIND_TOOL_RESULT, KIND_THINKING,
              KIND_SPINNER, KIND_AGENT}

    @staticmethod
    def _width(default: int = 80) -> int:
        try:
            return shutil.get_terminal_size((default, 24)).columns
        except Exception:
            return default

    def _bar(self, text: str, indent: str = "") -> str:
        """给**每一行**加竖线 —— 这是"框住多行内容"的关键。

        注意:传入的 text 必须是**已经去掉 PlainRenderer 那层缩进**的,
        否则会出现"竖线后还空一段"的双重缩进。
        """
        prefix = f"{self.BAR} {indent}" if indent else f"{self.BAR} "
        return "\n".join(prefix + line for line in str(text).split("\n"))

    def render(self, msg: Message) -> str | None:
        k = msg.kind

        if k == KIND_USER:
            return self._bar(paint("你 > ", GREEN) + msg.text)

        if k == KIND_DIVIDER:
            return paint("─" * self._width(), NEUTRAL)

        if k in self.BARRED:
            # 对话流:自己拼文本(不用 PlainRenderer,避免它那层缩进)
            if k == KIND_TOOL_CALL:
                body = (f"{paint('🛠 Agent 调用了', BLUE)} "
                        f"[{msg.tool}] 参数={msg.args}")
            elif k == KIND_TOOL_RESULT:
                body = f"{paint(f'📝 {msg.tool} 返回:', SLATE)} {msg.text}"
            elif k == KIND_THINKING:
                body = f"{paint('💭 思考:', GRAY)} {msg.text}"
            elif k == KIND_SPINNER:
                body = paint(f"🤔 {msg.text}", GRAY_DIM)
            elif k == KIND_AGENT:
                body = paint("🤖 Agent:", WHITE) + f" {msg.text}"
            else:  # pragma: no cover - BARRED 已穷举
                body = msg.text
            return self._bar(body, msg.indent)

        # 非对话流(note/warn/error/raw):保持原样
        return super().render(msg)


class SilentRenderer:
    """静音**对话流**,但保留 **raw 输出**。

    ============ 为什么 raw 不静音(2026-09-16 用户定,方案 B) ============
    两类输出性质不同:

      对话流(tool_call / tool_result / agent ...) —— "给人眼睛看的",
          测试时是噪音,静音掉最干净。
      命令输出(/help、会话列表、再见、工具确认面板 ... 走 raw) ——
          这是**程序 stdout**,性质等同于 `ls` 的输出。用户把 dummy 输出
          重定向到文件(`python main.py > log.txt`)时,这部分**应该保留**,
          否则文件会变成空的。

    所以 silent 只作用于"渲染器会改造的那部分",raw 原样放行。
    """

    def render(self, msg: Message) -> str | None:
        if msg.kind == KIND_RAW:
            return msg.raw          # 原样放行
        return None                 # 对话流静音


# ===============================================================
# 对外 API
# ===============================================================
class UI:
    """对外唯一入口。

    业务代码只用两种调用形态:
        ui.show(KIND_TOOL_CALL, tool=name, args=args)   # 拼 + 印
        ui.raw("原样输出")                                # 原样打印

    需要更细的控制(如让别的模块消费消息)时,可以:
        msg = ui.build(KIND_AGENT, text)   # 只拼
        ui.emit(msg)                       # 只印
    """

    def __init__(self, renderer) -> None:
        self.renderer = renderer

    # ---- ① 只拼 ----
    @staticmethod
    def build(kind: str, text: str = "", **kw) -> Message:
        return Message(kind=kind, text=text, **kw)

    # ---- ② 只印 ----
    def emit(self, msg: Message) -> None:
        out = self.renderer.render(msg)
        if out is None:
            return
        print(out)

    # ---- 拼 + 印(最常用) ----
    def show(self, kind: str, text: str = "", **kw) -> None:
        self.emit(self.build(kind, text, **kw))

    # ---- 原样输出(工具内部的确认面板等,不经渲染改造) ----
    def raw(self, text: str = "", end: str = "\n") -> None:
        """原样打印。**工具内部的人机交互提示**走这里 —— 它们不是对话流,
        不参与竖线渲染(见模块头"竖线的边界")。"""
        out = self.renderer.render(Message(kind=KIND_RAW, raw=text))
        if out is None:
            return
        print(out, end=end)

    # ---- 输入提示符(需要返回字符串给 input()) ----
    def prompt(self) -> str:
        return paint("你 > ", GREEN)


def _make_ui() -> UI:
    choice = os.environ.get("DUMMY_UI", "").strip().lower()
    if choice == "line":
        return UI(LineRenderer())
    if choice == "plain":
        return UI(PlainRenderer())
    if choice == "silent":
        return UI(SilentRenderer())
    # 未指定:非 TTY(测试/管道)时静默,避免污染断言
    try:
        import sys
        if not sys.stdout.isatty():
            return UI(SilentRenderer())
    except Exception:
        pass
    return UI(PlainRenderer())


ui = _make_ui()

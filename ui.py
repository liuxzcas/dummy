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
import re
import shutil
from dataclasses import dataclass, field
from typing import Any

from colors import (
    paint, paint_prompt, GREEN, BLUE, SLATE, WHITE, GRAY, GRAY_DIM,
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
        out = _strip_control(out)      # 纵深防御,见 _strip_control 注释
        trace_output(msg.kind, out)
        safe_print(out)

    # ---- 拼 + 印(最常用) ----
    def show(self, kind: str, text: str = "", **kw) -> None:
        self.emit(self.build(kind, text, **kw))

    # 原样输出(工具内部的确认面板等,不经渲染改造)
    def raw(self, text: str = "", end: str = "\n") -> None:
        """原样打印。**工具内部的人机交互提示**走这里 —— 它们不是对话流,
        不参与竖线渲染(见模块头"竖线的边界")。

        ============ 打印前剥控制序列(2026-11-17) ============
        用户报的"清屏"真凶:命令输出尾部带着 \\x1b[H\\x1b[2J\\x1b[3J,
        被存进 history,每次打印就真的清屏 + 清 scrollback。

        terminal 工具入口已经剥了(一处收口),这里是**纵深防御**:
        万一别处(旧会话恢复、粘贴内容、未来新工具)漏了一处,
        也不会真的把终端清掉。
        """
        out = self.renderer.render(Message(kind=KIND_RAW, raw=text))
        if out is None:
            return
        out = _strip_control(out)
        trace_output(KIND_RAW, out)
        safe_print(out, end=end)

    # ---- 输入提示符(需要返回字符串给 input()) ----
    # 用 paint_prompt 而非 paint:提示符的 ANSI 码要用 \001/\002 包裹,
    # 否则 readline 会把转义字节也算进宽度,导致光标错位(见 colors.paint_prompt)
    def prompt(self) -> str:
        return paint_prompt("你 > ", GREEN)


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


# ============================================================
# 线程安全的打印(2026-11-11 输入架构改造 第 1 步)
# ============================================================
# ============ 为什么需要 ============
# 改造目标(用户):"agent 处理 tool calling 时依旧等待用户输入,
# 用户可以在命令行输入和编辑任意东西。"
#
# 那意味着 agent 要跑在【后台线程】,主线程被 prompt_toolkit 的
# Application 占着渲染输入区。此时后台线程直接 print() 会把
# 输入区画花 —— 这正是用户此前报的"工具轮间清屏/历史被擦掉"
# 的同一类问题(两个写入方 + 一个在管理光标)。
#
# prompt_toolkit 官方解法是 run_in_terminal():它会先暂停渲染、
# 让出光标,打印完再恢复。
#
# ============ 为什么不是无脑用它 ============
# run_in_terminal 只在**有一个正在运行的 Application** 时才成立。
# dummy 现在(第 1 步)还没有 Application:主线程在 session.prompt()。
# 所以要按"当前有没有活跃 app"分流:
#   · 有 → run_in_terminal(改造第 3、4 步之后)
#   · 没有 → 直接 print(现在的行为,零变化)
#
# 现在把入口建好、留好分流点,第 3-5 步只需要让 get_app() 返回
# 真对象,输出侧不用再改。
_active_app = None


def set_active_app(app) -> None:
    """由 prompt_toolkit 应用注册自己(改造第 3 步调用)。"""
    global _active_app
    _active_app = app


def _printing_app():
    """返回"正在运行且需要接管打印"的 app;没有则 None。

    取 app.running 而不是只看 set_active_app —— 应用退出后
    run_in_terminal 会抛异常,必须退回直接打印。
    """
    app = _active_app
    try:
        return app if app is not None and app.is_running else None
    except Exception:
        return None


def safe_print(text: str, end: str = "\n") -> None:
    """线程安全的打印:有活跃 app 时暂停输入区,否则直接 print。

    所有 ui 输出(core.py 的 42 处 print 也走这里)最终都落到这。
    """
    global _last_after
    app = _printing_app()
    if app is None:
        print(text, end=end)
    else:
        try:
            from prompt_toolkit.application import run_in_terminal
            run_in_terminal(lambda: print(text, end=end), in_executor=False)
        except Exception:
            # 任何异常都不能让输出丢失 —— 退回直接打印
            print(text, end=end)
    # 记打印后的光标位置(诊断清屏用):下一次 trace_output 会拿它
    # 和"打印前"的位置比对,不一致就说明中间有人动了光标。
    if _TRACE:
        _last_after = _cursor_pos()


# ============================================================
# 输出诊断(2026-11-11, 用户报"清屏"至今未定位)
# ============================================================
# ============ 为什么要这个 ============
# 用户:"清屏问题还是存在,多发于'思考'之前,但不是每次'思考'
#      环节都会清屏。"
#
# 信息停在"多发于思考之前"——这只能证明它**不是**每次思考都发生,
# 还不足以定位。继续靠读代码猜没有意义,需要运行时事实。
#
# 做法:DUMMY_OUTPUT_TRACE=1 时,每次输出前把
#   时间 / 输出种类 / 首行摘要 / 终端尺寸 / 光标位置
# 追加到 logs/output_trace.log。
# 下次再清屏,把那份日志拿出来 —— 能精确看到"清屏前的最后一次
# 输出是什么、当时光标在哪"。
#
# ============ 为什么默认关闭 ============
# 查光标位置要给终端发 DSR 查询(\x1b[6n)并等回复,有几百微秒开销,
# 而且某些终端(重定向/管道)压根不回复。只在排障时开。
_TRACE = os.environ.get("DUMMY_OUTPUT_TRACE", "").strip().lower() in {
    "1", "true", "yes", "on"}
_TRACE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "logs", "output_trace.log")
# 上一次打印【之后】的光标位置 —— 与下一次打印【之前】比对,
# 不一致就说明中间有东西动了光标(清屏线索)
_last_after: str | None = None
# 上一次看到的【控制台缓冲区总行数】—— 变小 = 真清屏的铁证
_last_buf: int | None = None


# ============================================================
# stdout 探针:抓"谁在写清屏序列"(2026-11-16)
# ============================================================
# ============ 为什么需要 ============
# 用户报清屏,trace 抓到 cursor 从 34 跳回 1,但:
#   · 代码里搜不到 ESC[2J / ESC[3J / clear
#   · 关掉 msvcrt 监听器仍复现
#   · 进程只有两个线程
#
# 继续读代码猜没有意义。直接 hook sys.stdout.write:
# 只要写出的内容含"清屏/归位"类序列,就把**调用栈**记下来 ——
# 谁写的、在哪一行,一目了然。
#
# 检测的序列(都是"让已有内容消失"的):
#   \x1b[2J  清屏(光标不动)
#   \x1b[3J  连带清 scrollback  ← 这个最符合"往上滚也看不到"
#   \x1b[H / \x1b[1;1H  光标归位到左上角
#   \x1b[?1049h/l  切换备用屏幕(进去再出来 = 原内容没了)
#   \x1b[0J / \x1b[J  从光标清到屏幕末尾
_CLEAR_SEQS = ("\x1b[2J", "\x1b[3J", "\x1b[H", "\x1b[1;1H",
               "\x1b[0J", "\x1b[J", "\x1b[?1049h", "\x1b[?1049l")
_stdout_hooked = False


def _hook_stdout() -> None:
    """包一层 sys.stdout.write,抓到清屏序列就记调用栈。

    只记栈顶 6 层 —— 足够定位到具体文件行,又不至于刷屏。
    """
    global _stdout_hooked
    if _stdout_hooked:
        return
    _stdout_hooked = True

    import sys as _sys
    import traceback

    orig = _sys.stdout.write

    def patched(text):
        try:
            s = text if isinstance(text, str) else str(text)
            for seq in _CLEAR_SEQS:
                if seq in s:
                    stack = traceback.format_stack(limit=7)[:-1]
                    # 只留"文件名:行号 函数名"
                    frames = []
                    for fr in stack:
                        fr = fr.strip()
                        if fr.startswith("File "):
                            frames.append(fr)
                    with open(_TRACE_PATH, "a", encoding="utf-8") as f:
                        f.write("\n*** CLEAR SEQUENCE DETECTED ***\n")
                        f.write(f"    seq={seq!r}  payload_len={len(s)}\n")
                        for fr in frames[-6:]:
                            f.write(f"    {fr}\n")
                        f.write("\n")
                    # 同时打到屏幕,用户当场能看到
                    orig(f"\n*** 检测到清屏序列 {seq!r} —— 见 trace 日志 ***\n")
        except Exception:
            pass
        return orig(text)

    _sys.stdout.write = patched


def _cursor_pos() -> str:
    """尽力取光标位置(行,列);拿不到返回 "?"。

    ============ 为什么用 Console API 而不是 DSR 查询 ============
    DSR(\\x1b[6n)要给终端发查询并等回复,管道/重定向下会卡住。
    Console API 是同步调用,拿不到就返回 "?",不会阻塞。
    """
    st = _console_state()
    return st.get("cursor", "?") if st else "?"


def _console_state() -> dict | None:
    """取完整的控制台状态(诊断清屏用)。

    返回:
        cursor  "行,列"          光标位置
        buf     缓冲区总行数       ← ★ 清屏时这个会变小
        win     "Top..Bottom"    可见窗口范围
        height  可见高度

    ============ 为什么要 buf(2026-11-16) ============
    用户做过的 7 个隔离实验全部复现不了,说明 dummy 里还有未识别的因素。
    与其继续猜,不如直接盯住"缓冲区总行数":
      · 正常输出 → buf 只增不减(内容进 scrollback)
      · 真清屏   → buf 突然变小或被重置
    这一条数据能一刀切开"滚动"和"真清屏"。
    """
    if os.environ.get("DUMMY_TRACE_NO_CURSOR", "").strip().lower() \
            in {"1", "true", "yes", "on"}:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                        ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

        class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
            _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                        ("wAttributes", wintypes.WORD),
                        ("srWindow", SMALL_RECT),
                        ("dwMaximumWindowSize", COORD)]

        h = ctypes.windll.kernel32.GetStdHandle(-11)   # STD_OUTPUT_HANDLE
        info = CONSOLE_SCREEN_BUFFER_INFO()
        if not ctypes.windll.kernel32.GetConsoleScreenBufferInfo(
                h, ctypes.byref(info)):
            return None
        return {
            "cursor": f"{info.dwCursorPosition.Y},{info.dwCursorPosition.X}",
            "buf": info.dwSize.Y,
            "win": f"{info.srWindow.Top}..{info.srWindow.Bottom}",
            "height": info.srWindow.Bottom - info.srWindow.Top + 1,
        }
    except Exception:
        return None


def trace_output(kind: str, text: str) -> None:
    """DUMMY_OUTPUT_TRACE=1 时记一条输出日志(不影响正常输出)。

    ============ 记录格式(2026-11-12 追查清屏) ============
        时间  pid  kind  before=打印前光标  after=打印后光标
              jump=下一次打印前光标   size / rows / len / 首行

    ============ 为什么要有 after 和 jump ============
    用户 trace 里的铁证:
        538  kind=tool_result  cursor=34,0
        539  kind=thinking     cursor=1,0    ← 打印前已在第 1 行
    这证明跳变发生在两次输出之间,但**看不出是谁动的**。

    补上 after(这次打印完光标在哪)和 jump(下次打印前在哪):
        after 和 jump 不一致 → 中间有人移动了光标
        每行输出后立刻比对,**跳变当场打警告到屏幕**,用户复现时
        能直接看到"就在这一刻跳的"。
    """
    if not _TRACE:
        return
    global _last_after, _last_buf
    try:
        import datetime
        import shutil
        size = shutil.get_terminal_size((80, 24))
        first = (text or "").strip().split("\n")[0][:100]
        cols = max(20, size.columns)
        rows = 0
        for ln in (text or "").split("\n"):
            rows += max(1, -(-len(ln) // cols))

        st = _console_state() or {}
        before = st.get("cursor", "?")
        buf = st.get("buf")
        win = st.get("win", "?")

        # ============ 缓冲区行数缩小 = 真清屏的铁证 ============
        # 正常输出只增不减(内容进 scrollback);buf 变小说明缓冲区
        # 被重建/重置 —— 那才是"前面内容滚不到"的机械原因。
        shrink = ""
        if _last_buf is not None and buf is not None and buf < _last_buf:
            shrink = f"  **BUF_SHRINK {_last_buf}->{buf}**"
        if buf is not None:
            _last_buf = buf

        os.makedirs(os.path.dirname(_TRACE_PATH), exist_ok=True)
        new_file = not os.path.exists(_TRACE_PATH)
        with open(_TRACE_PATH, "a", encoding="utf-8") as f:
            if new_file:
                f.write(
                    f"===== SESSION pid={os.getpid()} "
                    f"start={datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
            f.write(
                f"{datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]}  "
                f"pid={os.getpid():<6} kind={kind:<12} "
                f"cursor={before:<9} buf={str(buf or '?'):<7} win={win:<9} "
                f"size={size.columns}x{size.lines}  "
                f"rows={rows:<3} len={len(text or '')}{shrink}  {first}\n")
        if shrink:
            print(f"{shrink.strip()}", flush=True)
    except Exception:
        pass


# ============================================================
# 打印前剥掉终端控制序列(2026-11-17)
# ============================================================
# ============ 为什么打印层还要再剥一次 ============
# 真凶已经定位:命令输出尾部带着 \x1b[H\x1b[2J\x1b[3J
# (\x1b[2J 清屏、\x1b[3J 连 scrollback 一起清、\x1b[H 光标归位),
# 存进 history 后每次打印就真的清屏 —— 用户"滚不到"就是这么来的。
#
# tools/terminal.py 的入口已经剥了(一处收口)。这里是**纵深防御**:
#   · 旧会话从 db 恢复,内容里早就带着这些序列
#   · 粘贴进来的文本、未来新增的工具都可能漏
# 打印是最后一道关 —— 只要它挡住了,终端就不会被清。
_CTRL_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]"              # CSI(含清屏 2J/3J、归位 H)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[@-Z\\-_]"                      # 两字节转义
)


def _strip_control(text: str) -> str:
    """剥掉控制序列(无 ESC 时零开销直接返回)。"""
    if not text or "\x1b" not in text:
        return text
    return _CTRL_RE.sub("", text)


ui = _make_ui()
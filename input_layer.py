"""输入层 —— 统一的用户输入入口。

============ 为什么要这一层(2026-10-16) ============
现在 dummy 各处直接调 `input()`(main.py 主循环、core.py 的确认/打断收集)。

**为什么不能一直用裸 input()**:
    Python 的 `input()` 有一个根本限制 —— **换行符就是它的结束标记**。
    用户粘贴多行文本时,`input()` 只返回第一行,其余行变成"下一次输入",
    于是**一段话被拆成多条独立消息**。

    实测(见 C:\\...\\hermes-multiline-probe.py):
        粘贴 "第一行\\n第二行\\n第三行" → input() 三次返回三行,
        且每次返回的字符串里**不含换行符**(被当成结束标记消耗掉)。

    Hermes 的解法是换掉 input() —— 用 prompt_toolkit 的
    `Keys.BracketedPaste` 事件,整个粘贴块作为**一个事件**到达。

**为什么现在还不换**:
    换 prompt_toolkit 会牵动输出层(ui.py 的 print 渲染)、
    /p 打断监听(现靠后台线程轮询键盘)、以及大量 mock input 的测试。
    所以分两步:
        第一步(本文件): 收口成 read_input(),在此之上加【拖拽路径检测】等能力
        第二步(将来):   把 read_input() 的实现换成 prompt_toolkit,
                        顺带解决多行粘贴 + Win11 吞字 + 行编辑

**所以本文件的价值是"唯一的改造点"** —— 换库时只改这里,
调用方(main.py / core.py)一行都不用动。
"""
import os
import sys

from colors import paint, GRAY_DIM, YELLOW   # noqa: E402


# ============================================================
# 斜杠命令白名单
# ============================================================
# ============ 为什么需要白名单(2026-10-16) ============
# 拖拽文件时,终端把路径作为文本插入,例如:
#         C:\Users\me\a.txt        或        /c/Users/me/a.txt
# 而 dummy 有 14 个斜杠命令,都以 "/" 开头 ——
# **git-bash 风格路径 `/c/Users/...` 与命令前缀相同**。
#
# Hermes 的教训(cli.py:2869 注释原文):
#     "`starts_like_path` prefilter accepts any input starting with `/`,
#      then this resolver tries to stat it ... Without this guard the
#      OSError propagates up to the process_loop catch-all and the user
#      input is silently lost (the warning ends up in agent.log but the
#      user sees nothing — the prompt just hangs)."
#   → 它靠"文件必须真的存在"兜底,但仍踩过"输入静默丢失"的坑。
#
# dummy 的选择:**先查白名单** —— 是已知命令就绝不走路径分支。
# 这样 `/c/Users/me/a.txt`(不在白名单)才可能被当成路径。
KNOWN_COMMANDS = frozenset({
    "/quit", "/exit", "/q", "/help", "/reset", "/resume", "/sessions",
    "/history", "/search", "/memories", "/skills", "/lessons", "/improve",
    "/tools",
})


def is_known_command(text: str) -> bool:
    """输入是否是已知斜杠命令(含带参数的,如 `/lessons -a`)。"""
    parts = text.strip().split()
    if not parts:
        return False
    return parts[0].lower() in KNOWN_COMMANDS


# ============================================================
# 拖拽路径检测
# ============================================================
def looks_like_path(text: str) -> bool:
    """快速预筛:这个字符串**可能**是路径吗?

    只是性能优化(避免对每句话都做文件系统 stat)。
    真正判定靠 `os.path.isfile()` —— 见 detect_file_drop。

    覆盖形态:
        /c/Users/me/a.txt     git-bash 绝对路径
        C:\\Users\\me\\a.txt    Windows 绝对路径
        C:/Users/me/a.txt     Windows 正斜杠
        ./a.txt  ../a.txt     相对路径
        ~/a.txt               家目录
        "C:\\path with space\\a.txt"   带引号(资源管理器拖拽常带)
        'C:\\path with space\\a.txt'
    """
    s = text.strip()
    if not s:
        return False
    # 带引号:去引号后再判
    if s[0] in "\"'" and len(s) > 2:
        s = s[1:]
    if s.startswith(("/", "~", "./", "../")):
        return True
    # Windows 盘符:C:\ 或 C:/
    if len(s) >= 3 and s[1] == ":" and s[2] in ("\\", "/") and s[0].isalpha():
        return True
    return False


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _existing_file(candidate: str) -> str | None:
    """候选字符串是真实存在的文件就返回规范化路径,否则 None。"""
    p = _strip_quotes(candidate)
    if not p:
        return None
    try:
        # expanduser 处理 ~ ; normpath 统一斜杠;abspath 解析 ./
        p = os.path.abspath(os.path.expanduser(p))
        if os.path.isfile(p):
            return p
    except (OSError, ValueError):
        # 非法路径(空字节、超长、Windows 保留名等)一律当"不是路径"
        return None
    return None


def detect_file_drop(text: str) -> dict | None:
    """检测输入是否以**真实存在的文件路径**开头(拖拽/粘贴路径)。

    返回 {"path": 绝对路径, "remainder": 路径后面的文字} 或 None。

    ============ 判定顺序(每一步都可能否决) ============
    ① 斜杠命令白名单 —— 是命令就绝不走路径分支
    ② 形态预筛 —— 不像路径就别去 stat(省 syscall)
    ③ 真实存在 —— 只有 isfile() 通过才认(这才是可靠判据)

    ============ 为什么支持"路径 + 后续文字" ============
    用户拖拽文件后常接着打字:
        C:\\Users\\me\\log.txt 帮我看看这个报错
    整串不是文件,但前半段是。所以从后往前找空格切分点,
    取最长的、真实存在的前缀当路径,剩下的当 remainder。

    ============ 为什么不返回"读到的内容" ============
    用户 2026-09 定过:**拖拽只告知路径,让模型自己决定读不读、读多少**。
    自动读全文可能:① 撑爆上下文 ② 对二进制文件无意义。
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None

    # ① 斜杠命令优先(必须最先 —— 见 KNOWN_COMMANDS 的说明)
    if is_known_command(stripped):
        return None

    # ② 形态预筛
    if not looks_like_path(stripped):
        return None

    # ③ 整串就是路径?
    whole = _existing_file(stripped)
    if whole is not None:
        return {"path": whole, "remainder": ""}

    # ③b 路径 + 后续文字:从后往前试切分点
    #
    # 为什么要"从后往前":路径本身可能含空格
    #     C:\Program Files\app\log.txt 看看这个
    #   → 从第一个空格切会得到 "C:\Program",不存在;
    #     从 "log.txt" 后面那个空格切才对。
    positions = [i for i, ch in enumerate(stripped) if ch == " "]
    # 带引号的路径:先按引号切一次(资源管理器拖拽含空格的路径会加引号)
    if stripped[0] in "\"'":
        closing = stripped.find(stripped[0], 1)
        if closing > 0:
            quoted = _existing_file(stripped[:closing + 1])
            if quoted is not None:
                return {"path": quoted,
                        "remainder": stripped[closing + 1:].strip()}
    for pos in reversed(positions):
        candidate = stripped[:pos]
        hit = _existing_file(candidate)
        if hit is not None:
            return {"path": hit, "remainder": stripped[pos + 1:].strip()}
    return None


def format_drop_message(drop: dict) -> str:
    """把拖拽结果整理成**给模型看的**user 消息文本。

    ============ 为什么这样措辞(2026-10-16) ============
    原则:代码只提供事实,不替模型做决定。
      所以这里【不写】"请读取这个文件" —— 那是指令。
      只陈述:用户拖入了什么、路径是什么、以及他另外说了什么。
    要不要读、读多少,模型自己判断(它有 read_file 工具)。
    """
    path = drop["path"]
    remainder = drop.get("remainder") or ""
    try:
        size = os.path.getsize(path)
        size_txt = (f"{size} 字节" if size < 1024
                    else f"{size / 1024:.1f} KB" if size < 1024 * 1024
                    else f"{size / (1024 * 1024):.1f} MB")
    except OSError:
        size_txt = "大小未知"

    lines = [f"[用户拖入了文件] {path} ({size_txt})"]
    if remainder:
        lines.append(f"[用户同时说] {remainder}")
    return "\n".join(lines)


# ============================================================
# prompt_toolkit 会话(单例)
# ============================================================
# ============ 为什么换成 prompt_toolkit(2026-10-17) ============
# 根本原因:python 的 `input()` 以**换行符为结束标记**,
# 粘贴多行文本时只返回第一行,其余行变成"下一次输入" ——
# 一段话被拆成多条独立消息。实测:
#     粘贴 "第一行\n第二行\n第三行" → input() 三次返回三行,
#     且每次返回的字符串里**都没有换行符**(被当成结束标记消耗)。
#
# 换 prompt_toolkit 后:终端粘贴时发送 bracketed-paste 序列
#     \x1b[200~ <含换行的原始内容> \x1b[201~
# 它把这个整块作为 Keys.BracketedPaste 事件交付,**换行不触发提交**。
# 实测(create_pipe_input 模拟喂入):
#     收到 ('PASTE', '第一行\n第二行\n第三行'),含换行符、3 行 ✅
#
# 附带好处:prompt_toolkit 自己做行编辑与渲染,不经过 readline ——
# 绕开 pyreadline3 的两个 bug(Windows 中文吞字、粘贴切片)。
#
# ============ 为什么要"单例" ============
# PromptSession 持有输入历史与内部状态;每次 prompt() 都新建会丢历史,
# 且重复初始化按键绑定。全进程一个即可。
_session = None

# 本进程的模型名(用于"窗口 × 15%"分档);main.py 启动时 set_model_name 设置
_model_name: str | None = None

# 一档(内联)的待发送内容 —— 用户按回车时由 read_input 合并进返回值
_pending_inline: list[str] = []

# 二档(存文件)的待发送路径 —— 同样在 read_input 结束时合并
_pending_files: list[str] = []


def set_model_name(model: str | None) -> None:
    """登记当前模型名(供 token 分档用)。

    为什么不在 input_layer 里直接读环境变量:main.py 启动时可能让用户
    交互式输入模型名,环境变量未必反映最终值。由调用方显式告知最准。
    """
    global _model_name
    _model_name = model


def _current_model() -> str | None:
    """当前模型名;未设置时回退到环境变量。"""
    return _model_name or os.environ.get("DUMMY_AGENT_MODEL")


def _build_key_bindings():
    """构造按键绑定。

    BracketedPaste 必须 eager=True:否则内容会被当成普通按键逐字处理,
    换行又触发了提交 —— 正是我们要避免的。
    """
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    kb = KeyBindings()

    @kb.add(Keys.BracketedPaste, eager=True)
    def _on_paste(event) -> None:
        # ============ 粘贴的处理(2026-10-19 用户定的两档方案) ============
        # 一档(token 估算 < 模型窗口 × 15%):**直接内联**进 user 消息。
        #   用户的话:"如果临时文件内容是文本,就可以直接 call 一下
        #   read_file 或者更无痕的做法,它本身就是用户打算粘贴给 agent 的内容"。
        #   → 不落盘、没有"请读取文件"指令 —— 对模型来说等同于用户自己打的字。
        #   实测:内联的换行只是字符串里的 chr(10),不会被当成回车提交
        #   (终端输入阶段早已结束);dummy 也从不在屏幕上重放 user 消息
        #   (KIND_USER 全项目零调用),所以不会刷屏。
        #
        # 二档(≥ 15%):存文件 + 只给路径。
        #   → 避免一次塞几十万 token 打爆上下文。
        #
        # 两档都:清空行缓冲(不留冗杂占位符)、打一行灰字提示。
        text = event.data
        if not _is_long_text(text):
            # 短粘贴(单行且 <2000 字符):按正常输入插进缓冲区,用户可继续编辑
            event.app.current_buffer.insert_text(text)
            return

        # ============ 为什么不能 reset()(2026-10-30 用户实测) ============
        # 原来这里无脑 `current_buffer.reset()` 清空行缓冲 ——
        # 用户实测:"粘贴动作会直接抹掉用户原先打好的字"。
        # 正确做法:缓冲区保持不动(用户打的字还在,光标位置也不变),
        # 粘贴内容走 _pending_inline / _pending_files,
        # 回车时由 _compose_with_pending 合并进去。
        tokens = estimate_tokens(text)
        if tokens < _inline_threshold(_current_model()):
            # --- 一档:内联,不落盘 ---
            _pending_inline.append(text)
            notice = (f"  📋 已粘贴 {text.count(chr(10)) + 1} 行:"
                      f"「{_head_tail_excerpt(text)}」(直接发送)")
        else:
            # --- 二档:存文件,只给路径 ---
            path = _save_paste(text)
            _round_files.append(path)       # 轮末清理
            _pending_files.append(path)     # 回车时拼进 user 消息
            notice = (f"  📋 已存入 {os.path.basename(path)}"
                      f" ({text.count(chr(10)) + 1} 行, {len(text)} 字符)")
        event.app.invalidate()
        _emit_notice(notice)

    return kb


def _get_session():
    """惰性获取 PromptSession 单例;不可用时返回 None(调用方退回 input())。"""
    global _session
    if _session is not None:
        return _session
    try:
        from prompt_toolkit import PromptSession
        _session = PromptSession(key_bindings=_build_key_bindings())
        # 真正探测一次:构造成功不代表能用 —— output 是惰性创建的,
        # 要等到首次 prompt() 才可能抛 NoConsoleScreenBufferError。
        # 这里主动取一次 output 来暴露问题。
        _session.app.output.get_size()
        return _session
    except Exception:
        # ============ 为什么要有这个兜底(2026-10-17 实测) ============
        # prompt_toolkit 在这些环境里会抛 NoConsoleScreenBufferError:
        #   · git-bash / mintty(TERM=xterm-256color 但拿不到 Win32 控制台)
        #   · 任何 stdout 非控制台的场合(管道、重定向、被别的程序调起)
        # 而 dummy 可能在这些环境里被运行 —— 不该因此完全用不了。
        # 退回 input():功能少(无多行粘贴保护),但至少能用。
        _session = False        # False = 已探测过且不可用
        return None


def _read_via_prompt_toolkit(prompt: str) -> str | None:
    """用 prompt_toolkit 读一行;不可用返回 None。

    粘贴的处理在 _build_key_bindings 的 BracketedPaste 回调里完成 ——
    长文本在**粘贴瞬间**就落盘,占位符被塞进行缓冲区,
    所以这里拿到的 line 已经就是"占位符(+ 用户随后打的字)"。
    """
    session = _get_session()
    if not session:
        return None
    try:
        return session.prompt(prompt)
    except Exception:
        # 运行中途失败(如控制台被夺走)→ 本进程内不再尝试
        global _session
        _session = False
        return None


# ============================================================
# 统一输入入口
# ============================================================
def _strip_readline_markers(prompt: str) -> str:
    """去掉提示符里的 \\001/\\002(readline 的非打印标记)。

    ============ 为什么必须去(2026-10-17 用户实测截图) ============
    colors.paint_prompt() 在"有 readline"时会用 \\001/\\002 包裹 ANSI 码,
    那是**给 readline 的约定**(告诉它这段不计入提示符宽度)。

    prompt_toolkit **不认识**这两个字节 —— 它直接当可见字符渲染,于是
    终端上出现:
        ^A^[[38;2;152;195;121m^B你 > ^A^[[0m^C
        ↑↑                          ↑↑
       \x01(SOH)                   \x02(STX)

    实测(to_formatted_text(ANSI(...))):
        纯 ANSI 串  → [('#98c379','你'), ('#98c379',' '), ...]  ✅
        含 \001/\002 → [('[ZeroWidthEscape]','\x1b[...'), ...]   ❌ 控制字符被当文本

    所以:换成 prompt_toolkit 后,**这两个标记必须剥掉**。
    (prompt_toolkit 自己解析 ANSI,知道转义序列不占宽度 —— 不需要我们标。)
    """
    return prompt.replace("\x01", "").replace("\x02", "")


def _to_pt_prompt(prompt: str):
    """把着色提示符转成 prompt_toolkit 的 FormattedText。

    用 ANSI() 包装器解析 —— 它把 "\\x1b[38;2;...m文本\\x1b[0m" 正确拆成
    (样式, 文本) 片段列表,prompt_toolkit 据此渲染且宽度计算正确。
    """
    from prompt_toolkit.formatted_text import ANSI
    return ANSI(_strip_readline_markers(prompt))


# ============================================================
# 本轮临时文件的登记(供 core 在一轮对话结束时清理)
# ============================================================
# ============ 为什么需要登记(2026-10-19 用户反馈) ============
# 用户实测:"粘贴完文本之后,发送完消息可以自动清除临时文件。
#            现在临时文件完全不清。"
#
# 为什么不能"存了就删":paste 文件要等模型 read_file 之后才能删 ——
# 时序是:
#     粘贴 → 存文件 → (用户回车)发送 → 模型决定 read_file
#          → 读取成功 → 模型回复 → 本轮结束
#                      ↑ 只有到这一步才能删
# 所以生命周期 = "本轮对话",由 core 在 chat() 的 finally 里统一回收。
# (若真删早了,模型 read_file 会报文件不存在 —— 这必须避免。)
_round_files: list[str] = []


def take_round_files() -> list[str]:
    """取出并清空本轮登记的临时文件路径(core 在轮末调用)。"""
    files = list(_round_files)
    _round_files.clear()
    return files


def cleanup_files(paths: list[str]) -> int:
    """删除给定文件(忽略已不存在/无权限的)。返回成功删除数。"""
    n = 0
    for p in paths:
        try:
            os.remove(p)
            n += 1
        except OSError:
            pass
    return n


def read_input(prompt: str) -> str:
    """读取一行用户输入(含多行粘贴)。

    ============ 这是**唯一**的输入入口(2026-10-16) ============
    所有需要"用户打字"的地方都应走这里,而不是裸 input()。
    好处:换实现(本函数从 input() 换成 prompt_toolkit)时,
    调用方一行不动 —— 2026-10-17 就是这么换的。

    ============ 返回值语义(多行粘贴时) ============
    用户粘贴一段多行文本后回车,返回的是**整段内容**(含换行)。
    如果用户先粘贴再接着打字,则是"粘贴内容 + 后续输入"拼接。
    (顺序上:粘贴内容在前 —— 因为粘贴发生在输入过程中,而
     `line` 只包含按键敲出来的部分。)

    ============ 提示符处理(两个后端不一样) ============
    调用方传进来的是 colors.paint_prompt() 的产物 —— 它可能含
    \\001/\\002(readline 的非打印标记)。
      · prompt_toolkit:剥掉标记,用 ANSI() 包装(自己算宽度)
      · 兜底 input():保留原样(readline 需要这两个标记)
    详见 _strip_readline_markers 的说明。
    """
    _pending_inline.clear()
    _pending_files.clear()
    line = _read_via_prompt_toolkit(_to_pt_prompt(prompt))
    if line is not None:
        return _compose_with_pending(line)
    # 兜底:prompt_toolkit 不可用(git-bash/管道/无控制台)→ 退回 input()。
    # 代价:拿不到多行粘贴块(换行被当结束标记),但至少能用。
    # 提示符保持原样 —— readline 需要 \001/\002 才不会算错宽度。
    return _compose_with_pending(input(prompt))


def _compose_with_pending(typed: str) -> str:
    """把粘贴内容合并进用户输入,返回最终要发给模型的文本。

    ============ 两档的合并方式不同(2026-10-19 用户定) ============
    一档(内联):直接把原文拼上 —— 对模型来说等同于用户自己打的字,
                没有任何"这是文件"的痕迹。
    二档(存文件):拼上**一句事实陈述 + 路径**,不塞正文。
                措辞刻意不含指令("请读取"之类)—— 按项目原则,
                代码只摆事实,读不读归模型判断。

    用户打的字可能为空(粘贴完直接回车):这时也要能发出消息,
    所以 main.py 的"空输入就 continue"要配合 has_pending 判断。
    """
    parts = []
    if typed.strip():
        parts.append(typed)
    parts.extend(_pending_inline)
    for path in _pending_files:
        try:
            lines = sum(1 for _ in open(path, encoding="utf-8",
                                        errors="replace"))
            size = os.path.getsize(path)
            size_txt = (f"{size} 字节" if size < 1024
                        else f"{size / 1024:.1f} KB" if size < 1024 * 1024
                        else f"{size / (1024 * 1024):.1f} MB")
        except OSError:
            lines, size_txt = 0, "大小未知"
        parts.append(
            f"[用户粘贴了一段长文本] 原文已保存到 {path}"
            f"(共 {lines} 行, {size_txt}, 未直接放入对话)")
    _pending_inline.clear()
    _pending_files.clear()
    return "\n".join(parts)


def read_plain(prompt: str, agent=None) -> str:
    """读一行**普通**输入(确认/问答/工具内部提问),不走拖拽/长文本逻辑。

    ============ 为什么还需要这个函数(2026-10-19 用户实测) ============
    用户:粘贴 URL 时仍偶发丢字,"问题出在 web_search 工具询问用户输入
          api key 时"。

    根因同主循环那次:项目里还有几处**裸调 input()** 的地方 ——
        · tools/web_search.py   问 API key
        · core.py               执行确认 y/n、会话锁冲突、/p 打断后收集提示词
    它们都在【工具执行期间或之后】发生,此时 /p 监听线程是**活的**,
    照样和输入抢同一个控制台。

    所以在输入层统一收口:凡是要读用户输入,都走这里 ——
    它会 pause/resume 监听器,消除竞争。

    与 read_input 的区别:
      · read_input:主对话输入,要识别拖拽路径 + 长文本转文件
      · read_plain:确认/提问,原样返回(去首尾空白由调用方决定)
    """
    listener = getattr(agent, "_interrupt_listener", None) if agent else None
    if listener:
        listener.pause()
    try:
        return read_input(prompt)
    finally:
        if listener:
            listener.resume()


def read_input_with_drop(prompt: str, agent=None) -> tuple[str, dict | None]:
    """读一行输入,并检测是否拖入了文件。

    返回 (原始输入, drop 信息或 None)。

    ============ agent 参数是干嘛的(2026-10-19 修丢字 bug) ============
    用户实测:粘贴单行短文本会被截断成 "https://io1#"、"ht:c1n" 之类 ——
    **字符被吞掉**。同一现象的另一个表现是他早先报的"中文输入吞字"。

    根因:core.DummyAgent 里有个 _InterruptListener 后台线程,持续用
    msvcrt.kbhit()/getwch() **抢控制台输入**(为了支持工具运行中按 /p 打断)。
    而 prompt_toolkit 的 Win32Input 也在读同一个控制台 →
    **两边分吃同一个字符流** → 丢字。

    修法:读输入期间让监听器 pause(),读完 resume()。
    (原来只有 _confirm_provider 里 pause —— 主循环漏了。)

    传 agent=None 时不做 pause(测试/无 agent 场景)。
    """
    listener = getattr(agent, "_interrupt_listener", None) if agent else None
    if listener:
        listener.pause()
    try:
        raw = read_input(prompt)
    finally:
        if listener:
            listener.resume()
    drop = detect_file_drop(raw)
    return raw, drop


# ============================================================
# 长文本 → 临时文件
# ============================================================
# ============ 为什么要这一步(2026-10-16 用户定,10-17 实现) ============
# 用户原话:"大段文本有两个目的,一是避免填满对话窗口,
#           二是处理多行文本中的换行符 —— 避免被误读为回车、
#           误触发发送消息。"
#
# **之前做不到**:input() 把换行当结束标记吃掉,返回值里永远没有 \n
# → "含换行符"这个判据根本不可达。
# **换 prompt_toolkit 后**:BracketedPaste 的 event.data 原样保留换行
# → 判据第一次真正成立。
#
# 阈值沿用 Hermes(cli.py:14166 `paste_collapse_threshold` = 5 行 /
# `paste_collapse_char_threshold` = 2000 字符),但判据改成用户定的口径:
#     **含换行符** 或 **≥2000 字符** → 都算长文本
# (用户 2026-10-16:"包含换行符的文本连同 2000 字以上的文本统统定位长文本")
PASTE_CHAR_THRESHOLD = 2000

# 临时文件目录:项目根下 pastes/(与 logs/ 同级,便于用户找到和清理)
_PASTE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pastes")
_paste_seq = 0       # 本次进程内的序号(用于文件名)


def _paste_filename(text: str) -> str:
    """生成 paste_<时间戳>_<序号>.txt。

    ============ 为什么改成时间戳+序号(2026-10-19 用户定) ============
    用户:"文件名可以靠系统来分配,更加安全,不必是剪贴板内容的头几个字。"
          "时间戳+序号。反正对完话就清了,不需要可读性。"

    好处:
      · 文件名完全由系统控制 —— 内容里的任何字符都不会影响路径
        (旧做法要剃掉 \\ / : * ? " < > |,还可能撞上保留名 CON/NUL)
      · 不含用户内容 → 不会因为文件名泄露内容
    提示信息里仍然给首尾摘要,辨认靠提示不靠文件名。
    """
    global _paste_seq
    _paste_seq += 1
    import datetime
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(_PASTE_DIR, f"paste_{stamp}_{_paste_seq:03d}.txt")


def _is_long_text(text: str) -> bool:
    """是否该当"长文本"处理:含换行符 OR ≥2000 字符。

    用户 2026-10-16 定的口径:"包含换行符的文本连同 2000 字以上的文本
    统统定位长文本"。
    """
    if not text:
        return False
    return ("\n" in text or "\r" in text
            or len(text) >= PASTE_CHAR_THRESHOLD)


# ============================================================
# token 粗估 + 分档判定(2026-10-19 用户定)
# ============================================================
# ============ 为什么按 token 而不是字符数分档 ============
# 用户在 ② 里定的口径:"无痕读取的体积阈值按模型上下文能力来定,
# 比如以官方给定或用户设定的上下文窗口长度的 15% 来定"。
#
# 为什么用粗估系数而不是 tiktoken:
#   · 这是【分档】决定,不是精确计费 —— 误差几倍都不改变结论
#   · 零依赖(不引入 tiktoken/transformers)
#   · dummy 现有的 compressor 也是按字符数估的,口径一致
#
# 系数来源(业界常用经验值):
#   · CJK(中日韩)字符 ≈ 1.5 token / 字
#   · ASCII 等其他字符 ≈ 0.25 token / 字
# 混合内容按 CJK 占比加权。
INLINE_RATIO = 0.15          # 窗口的 15% 以内 → 直接内联


def estimate_tokens(text: str) -> int:
    """粗估 token 数(不依赖分词库)。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff"
              or "\u3040" <= ch <= "\u30ff"       # 日文假名
              or "\uac00" <= ch <= "\ud7af")      # 韩文
    other = len(text) - cjk
    return int(cjk * 1.5 + other * 0.25)


def _inline_threshold(model: str | None) -> int:
    """一档/二档的分界(单位:token)= 模型窗口 × 15%。"""
    from llm import resolve_window_tokens
    return int(resolve_window_tokens(model) * INLINE_RATIO)


def _head_tail_excerpt(text: str, n: int = 12) -> str:
    """取首尾各 n 个字做摘要,中间用 … 连起来。

    用户 2026-10-19:"一档添加开头和结尾各几个字,好比说
    '已粘贴47行:"致尊敬的...此致敬礼"(直接发送)'"。
    """
    flat = " ".join(text.split())        # 压掉换行/连续空白
    if len(flat) <= n * 2:
        return flat
    return f"{flat[:n]}…{flat[-n:]}"


def _save_paste(text: str) -> str:
    """把长文本写进 pastes/,返回文件路径。"""
    os.makedirs(_PASTE_DIR, exist_ok=True)
    path = _paste_filename(text)     # 内部已自增序号
    # newline="":按传入的原样写入,不做平台换行转换 ——
    # 用户粘贴的内容(可能来自网页/PDF)应该原样保留。
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path



def _emit_notice(notice: str) -> None:
    """粘贴后的屏幕反馈(灰色,与对话文字区分)。

    ============ 为什么不用 ui.raw(2026-10-19) ============
    这个函数在 prompt_toolkit 的按键回调里执行 —— 此时 prompt_toolkit
    正持有屏幕控制权。直接 print 会被它覆盖/错位。
    run_in_terminal 会在安全时机把输出插进去并重绘界面。
    """
    try:
        from prompt_toolkit.application import run_in_terminal
        from colors import paint, GRAY_DIM

        msg = paint(notice, GRAY_DIM)

        def _show() -> None:
            print(msg)

        run_in_terminal(_show)
    except Exception:
        # 不在 prompt_toolkit 上下文里(如测试)→ 直接打印
        try:
            from colors import paint, GRAY_DIM
            print(paint(notice, GRAY_DIM))
        except Exception:
            pass



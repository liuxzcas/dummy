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
# 统一输入入口
# ============================================================
def read_input(prompt: str) -> str:
    """读取一行用户输入。

    ============ 这是**唯一**的输入入口(2026-10-16) ============
    所有需要"用户打字"的地方都应走这里,而不是裸 input()。
    好处:将来换成 prompt_toolkit(解决多行粘贴 + Win11 吞字 +
    跨平台行编辑)只需改这个函数体,调用方一行不动。

    ============ 为什么提示符用 paint_prompt ============
    提示符含 ANSI 色码。在 readline 环境下必须用 \\001/\\002 包裹,
    否则 readline 会把转义字节算进宽度 → 光标错位。
    详见 colors.paint_prompt 的说明。

    ============ 已知限制(下一步解决) ============
    `input()` 以换行符为结束标记 —— **多行粘贴会被拆成多次返回**。
    实测:LF 分隔的 3 行文本 → input() 返回 3 次,每次一行,
    且每次的字符串里都没有换行符。
    → 多行文本/大段粘贴的处理,依赖 prompt_toolkit 的 BracketedPaste,
      见本模块头部说明。
    """
    return input(prompt)


def read_input_with_drop(prompt: str) -> tuple[str, dict | None]:
    """读一行输入,并检测是否拖入了文件。

    返回 (原始输入, drop 信息或 None)。

    调用方(main.py)的用法:
        raw, drop = read_input_with_drop(ui.prompt())
        if drop:
            user_input = format_drop_message(drop)
        else:
            user_input = raw.strip()
    """
    raw = read_input(prompt)
    drop = detect_file_drop(raw)
    return raw, drop

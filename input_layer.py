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
# 最近一次 prompt() 期间收到的粘贴块(可能多段:用户可粘贴多次)
_paste_chunks: list[str] = []


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
        # event.data 是**原始内容**(含换行),原样存下,不插入行缓冲
        _paste_chunks.append(event.data)

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
    """用 prompt_toolkit 读一行;不可用返回 None。"""
    session = _get_session()
    if not session:
        return None
    try:
        _paste_chunks.clear()
        line = session.prompt(prompt)
        if _paste_chunks:
            return "".join(_paste_chunks) + line
        return line
    except Exception:
        # 运行中途失败(如控制台被夺走)→ 本进程内不再尝试
        global _session
        _session = False
        return None


# ============================================================
# 统一输入入口
# ============================================================
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

    ============ 提示符 ============
    调用方传进来的是已经着色的字符串(colors.paint_prompt)。
    prompt_toolkit 自己解析 ANSI、知道哪些字节不占宽度,
    所以**不需要** readline 的 \001/\002 包裹 —— paint_prompt 里
    那套判断在 prompt_toolkit 下会退化成纯色码,无害。
    """
    _paste_chunks.clear()
    line = _read_via_prompt_toolkit(prompt)
    if line is not None:
        return line
    # 兜底:prompt_toolkit 不可用(git-bash/管道/无控制台)→ 退回 input()。
    # 代价:拿不到多行粘贴块(换行被当结束标记),但至少能用。
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
    """生成 <前20字>.txt(重名时加 _2 / _3 后缀)。

    ============ 为什么文件名就是"内容前 20 字"(用户 2026-10-16 要求) ============
    用户描述 Hermes 的体验:"你能看到一个灰色的 'xxx.txt' 文件名,
    'xxx' 是你复制的内容的第一行里截断出的前 20 个字"。
    → 文件名本身就要让用户**一眼认出**这是刚才粘的那段。

    ============ 为什么去掉了序号和时间戳(2026-10-17 改) ============
    初版是 `paste_1_20261029_201715_<前20字>.txt` —— 实测太长了,
    20 个字被序号和时间戳淹没,反而认不出来。
    现在:直接用内容前 20 字当文件名,重名才加 _2/_3。
    (同一段文字粘两次会得到 xxx.txt 和 xxx_2.txt,仍是可认的。)
    """
    import re

    # 文件名:去掉换行/回车/制表,以及 Windows 不允许的 \ / : * ? " < > |
    head = re.sub(r"[\r\n\t]", " ", text).strip()
    head = re.sub(r'[\\/:*?"<>|]', "", head)
    # 压缩连续空格,再截前 20 字
    head = re.sub(r"\s+", " ", head)[:20].strip()
    if not head:
        head = "pasted"

    path = os.path.join(_PASTE_DIR, f"{head}.txt")
    n = 2
    while os.path.exists(path):       # 重名 → 加 _2 / _3
        path = os.path.join(_PASTE_DIR, f"{head}_{n}.txt")
        n += 1
    return path


def maybe_collapse_paste(text: str) -> tuple[str, str | None]:
    """长文本 → 写临时文件,返回 (给模型看的文本, 文件路径或 None)。

    ============ 判据 ============
    含换行符 OR 字符数 ≥ PASTE_CHAR_THRESHOLD(2000)

    ============ 返回给模型什么 ============
    占位符 + 路径,**不塞全文**:
        [Pasted text #1: 47 lines → D:\\...\\pastes\\paste_1_....txt]
    模型要内容就用 read_file 自己读。
    → 遵循项目原则:代码提供事实("有段长文本存在这里"),
      读不读、读多少归模型判断。
    → 也顺带避免"一次塞 5 万字符把上下文打爆"。

    ============ 为什么短文本原样返回 ============
    2-4 行的短粘贴(如复制一个命令、一段引用)直接给模型更顺手 ——
    不必为它写文件、再让模型多花一次 read_file 调用。
    """
    if not text:
        return text, None

    has_newline = "\n" in text or "\r" in text
    if not (has_newline or len(text) >= PASTE_CHAR_THRESHOLD):
        return text, None

    os.makedirs(_PASTE_DIR, exist_ok=True)
    path = _paste_filename(text)

    global _paste_seq
    _paste_seq += 1
    # newline="":按传入的原样写入,不做平台换行转换 ——
    # 用户粘贴的内容(可能来自网页/PDF)应该原样保留。
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)

    lines = text.count("\n") + 1
    placeholder = (
        f"[Pasted text #{_paste_seq}: {lines} 行 / {len(text)} 字符 → {path}]")
    return placeholder, path

"""
tools/terminal.py — terminal 工具实现

在本地 shell 中执行命令并返回输出。

=== 执行环境(2026-08-17 修复) ===

早期用 subprocess shell=True:Windows 上 = cmd.exe,而 LLM 被提示为
bash 习惯(路径 /d/、&&、; 分隔),cmd 不认 → 语法错误 → 返工浪费
token(实测:一次 mkdir 返工 3 次)。

现在:优先用 git-bash 执行(bash -lc "命令"):
- LLM 的 bash 知识最丰富,路径/管道/重定向/脚本一致,返工率大降
- 三平台逻辑统一(Linux/macOS 系统自带 bash),可移植
- bash 找不到(Windows 无 git)时回退 shell=True(cmd),不更差
- 输出首行标注 [SHELL: git-bash] / [SHELL: cmd],LLM 知道执行环境

=== 编码说明（Windows 兼容） ===

Windows 中文版默认编码是 GBK，而 git-bash 输出可能是 UTF-8。
如果使用 subprocess 的 text=True 参数，Python 会用系统编码（GBK）
解码输出，碰到非法字节会抛出 UnicodeDecodeError。

修复方案：用 text=False（返回 bytes），手动 decode("utf-8", errors="replace")，
非法字符被替换为 �，不会崩溃。

=== 确认机制（2026-08-14 定稿） ===

确认交互在 handler 内（展示命令 + 命令说明），但输入收集与 '/p'
拦截由 core 注入的 _confirm 函数统一处理（命中 /p 抛 InterruptSignal，
不返回给 handler）。_confirm 为 None（直接调用/测试）时跳过确认。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from colors import paint, YELLOW, CYAN

# env_probe 在项目根(terminal.py 位于 tools/ 下)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from env_probe import describe_environment   # noqa: E402
import workspace                            # noqa: E402
from .registry import InterruptSignal       # noqa: E402
from ui import ui                            # noqa: E402
from tools.tasks import tasks, kill_process_tree   # noqa: E402


def _find_bash() -> str | None:
    """定位可用的 bash 解释器。

    优先级:
    1. 手动配置的 git-bash(环境变量 DUMMY_BASH_PATH 可覆盖;
       默认值 = 开发机已验证的 git-bash 路径)——git-bash 是
       手动配置项,不做自动探测(2026-08-17 用户决策)
    2. 系统 PATH 中的 bash:其他机器默认命中 WSL 启动器
       (System32\\bash.exe,WSL 正常时可用);Linux/macOS 命中
       系统自带 bash
    3. 都没有 → None(回退 shell=True)

    ============ 为什么第 1 步必须返回**绝对路径**(2026-09-18 实测) ============
    实测: 直接让子进程执行 "bash"(靠 PATH 解析)会**被 WSL 劫持**:

        shutil.which("bash")
          → C:\\Users\\xlinz\\AppData\\Local\\hermes\\git\\usr\\bin\\bash.EXE
        subprocess.run(["bash", "-lc", "echo hi"])
          → stderr: WSL (25 - Relay) ERROR: CreateProcessCommon:818:
                    execvpe(/bin/bash) failed: No such file or directory
          → returncode: 1

    也就是说:Windows 会把裸命令名 "bash" 交给 WSL 启动器,而 WSL 里的
    bash 不可用(本机 WSL 已知损坏)—— 命令直接失败,且报错信息与真实
    原因(WSL 抢走)看起来不相关,极难排查。

    用绝对路径(C:\\Program Files\\Git\\bin\\bash.exe)可以绕开这一层。
    **不要把第 1 步改成 shutil.which("bash")** —— 那会重新引入这个问题。
    """
    configured = os.environ.get("DUMMY_BASH_PATH") or r"C:\Program Files\Git\bin\bash.exe"
    if configured and os.path.isfile(configured):
        return configured
    found = shutil.which("bash")
    if found:
        return found
    return None


DEFAULT_TIMEOUT = 120     # 默认超时(秒)。对齐 Claude Code(120) / Hermes(180) 的做法
MAX_TIMEOUT = 600         # 上限(秒)。对齐两家的硬上限(600)


def _run_shell(command: str, timeout: int = DEFAULT_TIMEOUT, task=None,
               cwd: str | None = None, on_poll=None):
    """执行命令:优先 git-bash,回退系统默认 shell。

    返回 (stdout_bytes, stderr_bytes, returncode, shell_name, timed_out,
          interrupted)。

    ============ 为什么要自己开线程读管道(2026-09-18 实测) ============
    最初用 subprocess.run(timeout=N),发现超时**不能精确中止**:
        实测:subprocess.run([bash,"-lc","sleep 20"], timeout=5)
              → TimeoutExpired 在 **20.4 秒**才触发
    原因:内部流程是「超时 → kill(只杀直接子进程) → 继续 communicate 收尾」,
    而 communicate 要等管道 EOF;被杀的 bash 的子进程还活着、攥着管道
    → 只能等它自然结束。

    改用 Popen + communicate(timeout=N) 后,又发现两个新问题(均实测):
      ① **TimeoutExpired.stdout 是 None** —— 输出还在管道里没读出来,
         所以"保留部分输出"落空。
      ② 超时后再调一次 communicate() **不会重新读管道**,而是继续阻塞
         → 实测多等 5 秒(设定 3 秒实际 8.8 秒)。

    最终方案:**自己开线程读管道**,主线程只负责计时与超时处理:
      · 读线程用 **read1()** —— 这一点是关键,见下方注释
      · 超时时杀整棵树 → 管道关闭 → 读线程自然退出
      · 超时前已经产生的输出**已经在 buf 里**,不会丢

    ============ 为什么必须用 read1 而不是 read(2026-09-18 实测) ============
    实测(同样的命令,只换读法):
        p.stdout.read(1)     → 4 秒内读到 b'HELLO\n'   ✅
        p.stdout.read(4096)  → 4 秒内读到 b''          ❌ 阻塞等填满缓冲区
        p.stdout.read1(4096) → 4 秒内读到 b'WORLD\n'   ✅ 有多少读多少

    BufferedReader.read(n) 会**阻塞直到读满 n 字节或 EOF**;
    read1(n) 则是"读一次系统调用返回多少就给多少"。对"边跑边输出、
    可能永远填不满缓冲区"的长命令,必须用 read1。
    """
    bash = _find_bash()
    if bash:
        argv = [bash, "-lc", command]
        shell_name = "git-bash"
    else:
        argv = command
        shell_name = "cmd" if os.name == "nt" else "sh"

    # ============ 为什么要 stdin=DEVNULL(2026-11-02 用户实测) ============
    # 用户报告:工具执行期间(/p 打断的场景 A)敲键盘完全无反应,
    #          /p 只在【本轮结束后】才作为下一轮输入出现。
    #
    # 根因:原来只给了 stdout/stderr 的 PIPE,没给 stdin ——
    #      子进程**继承父进程的控制台**,和 _InterruptListener 的
    #      msvcrt.kbhit() 轮询抢同一个输入源,导致父进程根本读不到键。
    #      (对比:确认提示时 /p 正常 —— 那时没有子进程争抢。)
    #
    # 给 DEVNULL 的理由:
    #   · terminal 工具的命令本来就是**非交互**的 —— 模型没有办法给
    #     子进程喂 stdin,真遇到需要输入的命令只会卡到超时
    #   · 断开继承后,控制台输入归还父进程,msvcrt 轮询才能看到 /p
    #   · 万一以后真要支持交互式命令,再单独加 stdin=PIPE + 转发
    kwargs = {"stdin": subprocess.DEVNULL,
              "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
              # ============ cwd 由调用方传入(2026-11-05 改) ============
              # 原来是死值 workspace.ensure();现在由 terminal_handler
              # 传 _session_cwd 进来,支持跨调用保持目录。
              "cwd": cwd or workspace.ensure()}
    if os.name == "nt":
        # 独立进程组(便于整组终止;但 Windows 上没有 killpg,实际靠 taskkill /T)
        kwargs["creationflags"] = 0x00000200   # CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True      # POSIX:setsid,配合 os.killpg 使用

    if bash:
        proc = subprocess.Popen(argv, **kwargs)
    else:
        proc = subprocess.Popen(argv, shell=True, **kwargs)

    if task is not None:
        task.pid = proc.pid

    # ---- 启动读线程(必须在 wait 之前,否则管道写满会死锁) ----
    buf = {"out": b"", "err": b""}

    def _reader(stream, key):
        try:
            while True:
                chunk = stream.read1(4096)     # read1:不等填满
                if not chunk:
                    break
                buf[key] += chunk
        except Exception:
            pass

    # daemon=True:读线程卡在 read1 阻塞时,不阻止进程退出
    t_out = threading.Thread(target=_reader, args=(proc.stdout, "out"), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, "err"), daemon=True)
    t_out.start()
    t_err.start()

    # ---- 侦察后代(★ 必须抢在 shell 退出前做) ----
    #
    # ============ 为什么不能等命令结束再探(2026-10-16 实测) ============
    # 实测对比(命令 `sleep 30 & echo ok`,后台进程):
    #     t+ 551ms  bash 活着  → 后代 = 4  ✅
    #     t+1282ms  bash 已退出 → 后代 = 0  ❌
    # 原理:Windows 按 ParentProcessId 递归找后代,**父进程一死链就断了**;
    #      而后台命令(`&`)会让 bash 立刻退出 —— 时间窗口只有几百毫秒。
    #
    # 为什么用**循环**而不是 sleep 固定时长:
    #   list_descendants 内部要拉起 PowerShell,本身耗时几百毫秒;
    #   等它返回时 bash 可能已经退出(实测 sleep(0.15) 就抢不到)。
    #   所以改为"趁热连探" —— 一旦探到后代就立刻收工。
    #
    # 代价:后台退出的命令最多多花 ~0.4 秒;前台命令第一次就探到,
    #      几乎无额外开销(而且前台本来也不会提前退出)。
    if task is not None:
        for _ in range(4):
            tasks.note_descendants(task)
            if task.descendants:
                break
            if proc.poll() is not None:
                break       # shell 已退出,再探也没用(链已断)
            time.sleep(0.1)

    # ---- 主线程计时(可被 /p 打断)----
    #
    # ============ 为什么不是直接 proc.wait(timeout)(2026-11-11 用户报障) ============
    # 用户:"autopilot 模式下 /p 不能终止 tool calling loop"
    #
    # 根因链:
    #   ① 非 autopilot 时,工具跑完会进 confirm → read_plain → prompt_toolkit
    #      在活动,用户敲的 /p 由它读到并返回 → confirm 检测到 → raise
    #      InterruptSignal。**这条路能打断。**
    #   ② autopilot 时 confirm 直接 return "y",全程没有输入调用;
    #      打断完全依赖 _InterruptListener 的 msvcrt.kbhit() 轮询。
    #   ③ 而 terminal 工具这里 proc.wait(timeout) 把主线程**彻底阻塞**,
    #      加上子进程正占着控制台 —— kbhit() 期间极不稳定。
    #
    # 所以把"一次性等待整个超时"改成"小步轮询":
    #   每 POLL_STEP 秒检查一次,顺便调用 on_poll 回调。
    #   回调由 core 注入(检查监听器 buffer),命中就杀掉子进程并抛
    #   InterruptSignal —— 让打断**立刻生效**,而不是等命令跑完。
    #
    # 注意:这一步只负责"能被打断",不改变超时语义。
    POLL_STEP = 0.05
    timed_out = False
    interrupted = False
    deadline = time.time() + timeout
    while True:
        if proc.poll() is not None:
            break                       # 正常结束
        if on_poll is not None and on_poll():
            interrupted = True
            kill_process_tree(proc.pid)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            break
        if time.time() >= deadline:
            timed_out = True
            # 杀整棵树 —— script 里的固定下一步,不问 LLM(见 tasks 模块头的分层原则)
            kill_process_tree(proc.pid)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()      # 兜底
            break
        time.sleep(POLL_STEP)

    # 给读线程一个极短的窗口把缓冲里剩的数据取完。
    #
    # ============ 为什么不 join(2026-09-18 实测) ============
    # 实测过三种收尾方式:
    #   a) t.join(timeout=2)         → 卡满 2 秒(读线程阻塞在 read1 上不退)
    #   b) 先 p.stdout.close() 再 join → **更糟**:close() 本身要等读线程放开锁,
    #                                   实测阻塞 27.38 秒(死锁)
    #   c) sleep(0.3) 后直接返回      → 3.44 秒完成,输出完整 ✅
    #
    # 原因:读线程阻塞在 read1() 上等数据,只要管道还有写端就可能等下去。
    # 主线程不需要等它 —— 已经读到的内容在 buf 里,而遗漏的只可能是
    # 进程被杀那一刻尚未刷出缓冲的极少量尾部输出(可接受)。
    time.sleep(0.3)

    return (buf["out"], buf["err"], proc.returncode, shell_name,
            timed_out, interrupted)


# 常见命令的简单说明(确认提示处显示,帮助用户快速理解命令作用)
COMMAND_HINTS = [
    (("ls", "dir", "tree", "ll"), "📂", "查看目录/文件列表"),
    (("grep", "rg", "findstr", "find"), "🔍", "在文件中搜索文本"),
    (("rm", "del", "erase", "rmdir"), "🗑️", "删除文件/目录(不可恢复,请谨慎)"),
]


def _command_hint(command: str) -> str | None:
    """返回命令的说明文字(带 emoji);无法识别时返回 None。"""
    cmd0 = command.strip().split()[0].lower() if command.strip() else ""
    for names, emoji, desc in COMMAND_HINTS:
        if cmd0 in names:
            return f"{emoji} {desc}"
    return None


# ============================================================
# 打断检查钩子(2026-11-11)
# ============================================================
# ============ 为什么需要它 ============
# 用户:"autopilot 模式下 /p 不能终止 tool calling loop"
#
# 非 autopilot 时,打断靠 confirm 里的 read_plain(prompt_toolkit 在活动,
# 用户敲的 /p 被它读到)。autopilot 下 confirm 直接返回 "y",全程没有
# 输入调用 —— 打断只剩 msvcrt 轮询这一条路,而 terminal 工具的
# proc.wait(timeout) 会把主线程彻底阻塞,期间轮询极不稳定。
#
# 解法:让长命令执行期间**主动**去问"是否收到 /p"。
# core 启动时把自己的检查函数装進來(它是唯一知道监听器在哪的人),
# 本模块只管调用 —— 不在 tools 层引入对 core 的依赖(会循环导入)。
_interrupt_probe = None


def set_interrupt_probe(fn) -> None:
    """由 core 注入:"现在是否收到 /p?" 的检查函数。"""
    global _interrupt_probe
    _interrupt_probe = fn


def _default_on_poll() -> bool:
    """没有注入时永远返回 False(不影响非交互场景,如单测)。"""
    return bool(_interrupt_probe and _interrupt_probe())


# ============================================================
# 跨调用的 cwd 保持(2026-11-05 实测后实现)
# ============================================================
# ============ 为什么需要 ============
# 用户日志分析(logs/conversation_20261105_190809.json):
#     terminal 调用 130 次,其中 117 次以 `cd` 开头,只有 13 次不带 cd。
#     cd 目标统计:
#         ×55  cd /d/ja/sjcj                           ← 用户的项目目录
#         ×29  cd .../skills/lesson-plan-generator/templates
#         ×10  cd .../garage/vt2
#         ...
#
# 根因:每次 terminal 调用都是**新的 bash 子进程**,上一次的 cd 不会保留。
# 所以模型每次都要先 cd 到目标目录 —— 白白烧掉大量轮次和 token。
#
# ============ 方案:借用 Hermes 的做法 ============
# Hermes(tools/environments/base.py:555 + local.py:_update_cwd):
#     在命令末尾追加 `pwd -P > <临时文件>`,执行完读回来自我更新 cwd。
# 优点:
#   · 不污染模型看到的输出(不像 echo 标记那样混进 stdout)
#   · `pwd -P` 解析软链接,拿真实路径
#   · `|| true` 保证 pwd 失败不影响原命令
#
# ============ 两个必须处理的坑(Hermes 代码里都有) ============
# ① Windows 路径翻译:git-bash 的 `pwd -P` 输出 /c/Users/x,
#    而 Popen(cwd=...) 需要 C:/Users/x —— 不翻译会导致 isdir 检查失败。
# ② cwd 被删:模型执行 `rm -rf` 删掉自己当前目录后,
#    下次 Popen(cwd=不存在) 会 FileNotFoundError,整个工具报废。
#    (Hermes 的 issue #17558 就是这个,它用 _resolve_safe_cwd 逐级上溯。)
_session_cwd: str | None = None
_CWD_FILE = os.path.join(tempfile.gettempdir(), "dummy-terminal-cwd.txt")


def _msys_to_native(path: str) -> str:
    """把 git-bash 风格路径(/c/Users/x、/tmp/x)转成 Windows 原生路径。

    ============ 为什么不能只做正则替换(2026-11-05 实测) ============
    最初只处理 /c/xxx、/cygdrive/c/xxx 这种盘符形式。实测发现
    git-bash 的 /tmp 是一个**独立映射**:

        pwd -P 输出  /tmp/tmpso9v_k61/sub
        Windows 上  C:/Users/xlinz/AppData/Local/Temp/tmpso9v_k61/sub

    /tmp 不在任何盘符下,正则匹配不到 → 原样返回 → isdir 失败
    → cwd 永远保持不住(而且只在 pwd 落在 /tmp 时才复现,很难查)。

    修法:优先调 git-bash 自带的 `cygpath -w` 做权威转换,
    失败再退回正则(只认盘符形式)。
    """
    if os.name != "nt" or not path:
        return path
    # 已经是 Windows 形式(C:\ 或 C:/)就直接返回
    if re.match(r"^[a-zA-Z]:[\\/]", path):
        return path
    # 已带 /cygdrive/ 前缀的,先剥掉再交给 cygpath
    # (实测:cygpath 会把 /cygdrive/e/foo 当相对路径,
    #  转成 C:\...\hermes\git\cygdrive\e\foo —— 完全错)
    p = re.sub(r"^/cygdrive/(?=[a-zA-Z]/)", "/", path)
    # 权威转换:cygpath 知道所有映射(/tmp、/c、$HOME …)
    try:
        r = subprocess.run(["cygpath", "-w", p], capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            out = r.stdout.strip()
            # 防御:转换结果必须是一个盘符开头的绝对路径
            if re.match(r"^[a-zA-Z]:[\\/]", out):
                return out
    except (OSError, subprocess.SubprocessError):
        pass
    # 兜底:只处理盘符形式
    m = re.match(r"^/(?:cygdrive/)?([a-zA-Z])(/.*)?$", p)
    if not m:
        return path
    drive = m.group(1).upper()
    rest = (m.group(2) or "").replace("/", "\\")
    return drive + ":" + (rest or "\\")


def _safe_cwd(path: str) -> str:
    """cwd 不存在时(被 rm 掉了)回退到最近的存在的父目录,最后兜底 garage。

    对应 Hermes 的 _resolve_safe_cwd:模型执行 `cd x && rm -rf ../x`
    之后,下一条命令的 Popen(cwd=已删除) 会抛 FileNotFoundError,
    整个 terminal 工具就废了,必须自愈。
    """
    cur = path
    while cur and not os.path.isdir(cur):
        parent = os.path.dirname(cur.rstrip("\\/"))
        if parent == cur:
            break
        cur = parent
    if cur and os.path.isdir(cur):
        return cur
    return workspace.ensure()


def _read_back_cwd() -> str | None:
    """读回命令执行后记录的 cwd(由 _marked_command 写入)。"""
    try:
        with open(_CWD_FILE, encoding="utf-8") as f:
            p = f.read().strip()
    except (OSError, FileNotFoundError):
        return None
    p = _msys_to_native(p)
    return p if p and os.path.isdir(p) else None


def _marked_command(command: str, shell_name: str) -> str:
    """在命令末尾追加 cwd 记录,**同时保留原命令的退出码**。

    ============ 为什么必须保留退出码(2026-11-05 实测) ============
    最初直接拼 `pwd -P > file || true` —— 结果整个脚本的退出码
    变成最后一条命令(0),原命令的失败被吞掉:

        执行 definitely-not-a-command-xyz  → 原本 rc=127
        拼上 || true 之后               → rc=0

    后果有两层:
      ① 测试抓不到失败(实测 test_dispatch_stats 直接红)
      ② **模型也会以为命令成功了** —— 比测试红更严重

    修法:先存 $?,跑完 pwd 再 `exit $__rc` 还原。
    """
    if shell_name == "git-bash":
        return (
            f"{command}\n"
            f"__dummy_rc=$?\n"
            f'pwd -P > "{_CWD_FILE}" 2>/dev/null || true\n'
            f"exit $__dummy_rc"
        )
    if shell_name == "cmd":
        # cmd.exe 用 `cd`(无参数)打印当前目录;%ERRORLEVEL% 同理要保
        return (command + chr(13) + chr(10)
                + "set __DUMMY_RC=%ERRORLEVEL%" + chr(13) + chr(10)
                + 'cd > "' + _CWD_FILE + '" 2>nul' + chr(13) + chr(10)
                + "exit /b %__DUMMY_RC%")
    return command
# ============ 为什么需要它 ============
# 用户要"全托管自动跑"(autopilot) —— 但全自动意味着没人拦着。
# 黑名单是最后一道保险:命中就**拒绝执行**(不等人),直接把
# 拒绝原因告诉模型,让它换别的做法。
#
# ============ 为什么只有这几条(用户原话) ============
# "危险操作定个黑名单吧" + "也可以将黑名单范围中的①的范围缩小"
# 原方案第 ① 条是"写到项目外",现在有了 garage 工作区,
# 临时产物都落在 garage 里,写别处才需要确认 —— 所以黑名单只留
# **真正不可逆 / 影响宿主**的:
#   ① 推远程且强推(git push --force / -f)
#   ② 关机/重启/改启动项(影响宿主 OS)
#   ③ 删根目录 / 删家目录
# 沙箱本来就是为了"搞坏了无所谓",列太长会妨碍它干活。
DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    # (正则, 原因)
    (r"\bgit\s+push\b.*(--force\b|(?<!\w)-f(?:\s|$))",
     "强制推送会不可逆地覆盖远程历史"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*[fd]x?[a-zA-Z]*)",
     "会不可逆地丢弃本地改动"),
    (r"\b(shutdown|reboot|halt|poweroff)\b",
     "会关闭/重启宿主系统"),
    (r"\bbcdedit\b",
     "会修改宿主启动配置"),
    (r"(^|\s)rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|/\*|~/\*)\s*$",
     "会删除根目录或家目录"),
    (r"\bmkfs(\.\w+)?\b",
     "会格式化磁盘"),
    (r"\bformat\s+[a-zA-Z]:",
     "会格式化磁盘"),
]


def _blocked_reason(command: str) -> str | None:
    """命中黑名单则返回拒绝原因,否则 None。"""
    import re

    for pattern, reason in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return reason
    return None


def terminal_handler(command: str, timeout: int | None = None, _confirm=None,
                     on_poll=None) -> str:
    """在本地 shell 中执行命令，返回输出。

    确认交互在 handler 内,但输入收集与 '/p' 拦截由 core 注入的
    _confirm 函数统一处理(命中 /p 抛 InterruptSignal,不返回给 handler)。
    _confirm 为 None(直接调用/测试)时跳过确认。

    timeout: 可选,单位秒。默认 120,最大 600。
    """
    # ---- 危险命令黑名单(先于确认,不等人) ----
    # 命中直接拒绝:这不是"问用户要不要",而是硬拦截。
    # 全托管(autopilot)时没人能回答确认,不拦的话就是全自动执行破坏性命令。
    blocked = _blocked_reason(command)
    if blocked:
        return (f"[拒绝执行] 该命令命中危险操作黑名单: {blocked}\n"
                f"  命令: {command[:200]}\n"
                f"  这属于不可逆或影响宿主系统的操作,不允许自动执行。\n"
                f"  如需合法操作,请拆解成更小、可回退的步骤。")

    # ---- 执行前确认 ----
    if _confirm is not None:
        # 走 ui.raw:这是"问用户话"的交互面板,不是对话流
        ui.raw(f"\n  {paint('⚠️ 即将执行:', YELLOW)} {command}")
        hint = _command_hint(command)
        if hint:
            ui.raw(f"     {hint}")
        choice = _confirm("    按 Enter 确认执行, 输入 n 取消: ").strip().lower()
        if choice == "n":
            return "[用户取消] 命令未执行"

    # ---- 解析 timeout(夹在 [1, MAX_TIMEOUT] 内) ----
    try:
        secs = int(timeout) if timeout is not None else DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        secs = DEFAULT_TIMEOUT
    secs = max(1, min(secs, MAX_TIMEOUT))

    # ---- 登记到进程表(执行前,这样卡死时表里已有记录) ----
    task = tasks.register(pid=0, command=command)

    # ---- 执行 ----
    try:
        # 优先 git-bash(bash -lc),回退系统默认 shell;首行标注执行环境
        # 注意:不用 text=True(Windows GBK 编码会崩),手动 utf-8 解码
        # ============ cwd 保持(2026-11-05) ============
        # 起点:_session_cwd(上次命令结束时的目录)或 garage(首次)。
        # 命令末尾追加 pwd 记录,执行完读回来更新 _session_cwd。
        # 这样模型只需在换目录时 cd 一次,后续命令自动在正确目录跑。
        global _session_cwd
        cwd = _safe_cwd(_session_cwd or workspace.ensure())
        marked = _marked_command(command, "git-bash" if _find_bash() else "cmd")
        on_poll = on_poll or _default_on_poll
        stdout_b, stderr_b, rc, shell_name, timed_out, interrupted = _run_shell(
            marked, secs, task, cwd=cwd, on_poll=on_poll)
        # ============ 命令执行期间收到 /p(2026-11-11) ============
        # 立刻抛 InterruptSignal 让工具循环停下来 —— 不用等命令跑完,
        # 也不等检查点。这是 autopilot 下唯一能打断长命令的路径
        # (那边没有 confirm 的输入环节)。
        if interrupted:
            raise InterruptSignal()
        new_cwd = _read_back_cwd()
        if new_cwd:
            _session_cwd = new_cwd

        tasks.finish(task, timed_out)
        # 命令结束后探查后代 —— 可能有"命令已退出但后台进程还在"的情况
        tasks.note_descendants(task)

        output_parts = [describe_environment(shell_name)]

        stdout = stdout_b.decode("utf-8", errors="replace").strip() if stdout_b else ""
        if stdout:
            output_parts.append(stdout)

        stderr = stderr_b.decode("utf-8", errors="replace").strip() if stderr_b else ""
        if stderr:
            output_parts.append(f"[STDERR]\n{stderr}")

        if timed_out:
            output_parts.append(
                f"[超时] 命令在 {secs} 秒内没有结束，已被终止（连同它启动的子进程）。"
                f"上面是终止前已经产生的输出（可能为空）。"
                f"如果这是一条需要长时间运行的命令（例如在大目录树里搜索），"
                f"可以缩小范围，或改用更快的查找方式。"
            )
            _append_if_any(output_parts, _lingering_note(task))
            return "\n".join(output_parts)

        if rc != 0:
            output_parts.append(f"[EXIT CODE: {rc}]")

        # 命令正常结束,但可能留了后台进程(如 `npm run dev &`)。
        # 如实报告 —— 是否处理交给模型(见 tasks.format_lingering_report 的分层原则)。
        _append_if_any(output_parts, _lingering_note(task))

        # output_parts 里至少有环境声明那条,所以不需要"无输出"分支。
        # (历史上这里曾写 `if output_parts else "(命令执行成功，无输出)"`,
        #  但自 [SHELL:] 声明加入后,output_parts 永远非空 —— 那个分支
        #  永远不会执行。2026-09-18 删除,避免误导。)
        return "\n".join(output_parts)

    except InterruptSignal:
        # ============ /p 打断必须原样穿透(2026-11-11) ============
        # 实测踩到:上面的 except Exception 把它兜成了
        #   "[错误] 命令执行失败: InterruptSignal: 用户 /p 打断"
        # 于是打断变成"工具报错"——模型收到这条错误,以为工具失败了,
        # 换个命令接着干,loop 根本停不下来。
        # 这正是用户报的"autopilot 下 /p 不能终止 tool calling loop"。
        tasks.finish(task, timed_out=True)
        raise
    except Exception as e:
        tasks.finish(task, timed_out=True)
        return f"[错误] 命令执行失败: {type(e).__name__}: {e}"


def _append_if_any(parts: list[str], text: str) -> None:
    """只在 text 非空时追加。

    不加这层判断的话,append("") 会在 join 时多出一个换行 ——
    实测:"true" 这种空输出命令的返回值会从 1 行变成 2 行,
    被 test_no_dead_branch_for_empty_output 抓到。
    """
    if text:
        parts.append(text)


def _lingering_note(task) -> str:
    """命令结束后检查残留进程,有则返回一段事实陈述;没有则返回空串。

    只在**本次命令**的记录上检查(不扫全局表)—— 这样报告的是
    "你刚跑的那条命令留下了什么",而不是历史累积的一大堆。

    分层原则(2026-09-18 用户定):
        · 超时后的**清理** = 机制,写进 script 流程,不问 LLM(见 _run_shell)
        · "留下了什么进程" = **信息**,报告给 LLM 由它判断要不要处理
    """
    from tools.tasks import format_lingering_report
    return format_lingering_report([task] if task.still_running() else [])

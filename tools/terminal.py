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
import shutil
import subprocess
import sys
import threading
import time

from colors import paint, YELLOW, CYAN

# env_probe 在项目根(terminal.py 位于 tools/ 下)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from env_probe import describe_environment   # noqa: E402
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


def _run_shell(command: str, timeout: int = DEFAULT_TIMEOUT, task=None):
    """执行命令:优先 git-bash,回退系统默认 shell。

    返回 (stdout_bytes, stderr_bytes, returncode, shell_name, timed_out)。

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

    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
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

    # ---- 主线程计时 ----
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # 杀整棵树 —— script 里的固定下一步,不问 LLM(见 tasks 模块头的分层原则)
        kill_process_tree(proc.pid)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()      # 兜底

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

    return buf["out"], buf["err"], proc.returncode, shell_name, timed_out


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


def terminal_handler(command: str, timeout: int | None = None, _confirm=None) -> str:
    """在本地 shell 中执行命令，返回输出。

    确认交互在 handler 内,但输入收集与 '/p' 拦截由 core 注入的
    _confirm 函数统一处理(命中 /p 抛 InterruptSignal,不返回给 handler)。
    _confirm 为 None(直接调用/测试)时跳过确认。

    timeout: 可选,单位秒。默认 120,最大 600。
    """
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
        stdout_b, stderr_b, rc, shell_name, timed_out = _run_shell(command, secs, task)

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
            return "\n".join(output_parts)

        if rc != 0:
            output_parts.append(f"[EXIT CODE: {rc}]")

        return "\n".join(output_parts) if output_parts else "(命令执行成功，无输出)"

    except Exception as e:
        tasks.finish(task, timed_out=True)
        return f"[错误] 命令执行失败: {type(e).__name__}: {e}"

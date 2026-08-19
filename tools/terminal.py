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

from colors import paint, YELLOW, CYAN


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
    """
    configured = os.environ.get("DUMMY_BASH_PATH") or r"C:\Program Files\Git\bin\bash.exe"
    if configured and os.path.isfile(configured):
        return configured
    found = shutil.which("bash")
    if found:
        return found
    return None


def _run_shell(command: str, timeout: int = 30):
    """执行命令:优先 git-bash,回退系统默认 shell。

    返回 (CompletedProcess, shell_name);shell_name 用于输出标注,
    让 LLM 知道实际执行环境。
    """
    bash = _find_bash()
    if bash:
        result = subprocess.run(
            [bash, "-lc", command],
            capture_output=True,
            text=False,
            timeout=timeout,
        )
        return result, "git-bash"
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=False,
        timeout=timeout,
    )
    return result, "cmd" if os.name == "nt" else "sh"


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


def terminal_handler(command: str, _confirm=None) -> str:
    """在本地 shell 中执行命令，返回输出。

    确认交互在 handler 内,但输入收集与 '/p' 拦截由 core 注入的
    _confirm 函数统一处理(命中 /p 抛 InterruptSignal,不返回给 handler)。
    _confirm 为 None(直接调用/测试)时跳过确认。
    """
    # ---- 执行前确认 ----
    if _confirm is not None:
        print(f"\n  {paint('⚠️ 即将执行:', YELLOW)} {command}")
        hint = _command_hint(command)
        if hint:
            print(f"     {hint}")
        choice = _confirm("    按 Enter 确认执行, 输入 n 取消: ").strip().lower()
        if choice == "n":
            return "[用户取消] 命令未执行"

    # ---- 执行 ----
    try:
        # 优先 git-bash(bash -lc),回退系统默认 shell;首行标注执行环境
        # 注意:不用 text=True(Windows GBK 编码会崩),手动 utf-8 解码
        result, shell_name = _run_shell(command)

        output_parts = [f"[SHELL: {shell_name}]"]

        stdout = result.stdout.decode("utf-8", errors="replace").strip() if result.stdout else ""
        if stdout:
            output_parts.append(stdout)

        stderr = result.stderr.decode("utf-8", errors="replace").strip() if result.stderr else ""
        if stderr:
            output_parts.append(f"[STDERR]\n{stderr}")

        if result.returncode != 0:
            output_parts.append(f"[EXIT CODE: {result.returncode}]")

        return "\n".join(output_parts) if output_parts else "(命令执行成功，无输出)"

    except subprocess.TimeoutExpired:
        return f"[错误] 命令执行超时（30 秒上限）：{command}"
    except Exception as e:
        return f"[错误] 命令执行失败: {type(e).__name__}: {e}"

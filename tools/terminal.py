"""
tools/terminal.py — terminal 工具实现

在本地 shell 中执行命令并返回输出。

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

import subprocess

from colors import paint, YELLOW, CYAN


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
        # 注意：不用 text=True（Windows GBK 编码会崩）
        # 改为手动用 utf-8 解码，errors='replace' 兜底非法字符
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=False,   # 返回 bytes，手动解码
            timeout=30,
        )

        output_parts = []

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

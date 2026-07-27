"""
tools/terminal.py — terminal 工具实现

在本地 shell 中执行命令并返回输出。

=== 编码说明（Windows 兼容） ===

Windows 中文版默认编码是 GBK，而 git-bash 输出可能是 UTF-8。
如果使用 subprocess 的 text=True 参数，Python 会用系统编码（GBK）
解码输出，碰到非法字节会抛出 UnicodeDecodeError。

修复方案：用 text=False（返回 bytes），手动 decode("utf-8", errors="replace")，
非法字符被替换为 �，不会崩溃。
"""

import subprocess


def terminal_handler(command: str) -> str:
    """在本地 shell 中执行命令，返回输出。"""
    # ---- 执行前确认 ----
    print(f"\n  ⚠️  即将执行: {command}")
    choice = input("    按 Enter 确认执行, 输入 n 取消: ").strip().lower()
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

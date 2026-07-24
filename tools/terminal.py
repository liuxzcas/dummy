"""
tools/terminal.py — terminal 工具实现

在本地 shell 中执行命令并返回输出。
"""

import subprocess


def terminal_handler(command: str) -> str:
    """在本地 shell 中执行命令，返回输出。"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

        output_parts = []

        stdout = result.stdout.strip()
        if stdout:
            output_parts.append(stdout)

        stderr = result.stderr.strip()
        if stderr:
            output_parts.append(f"[STDERR]\n{stderr}")

        if result.returncode != 0:
            output_parts.append(f"[EXIT CODE: {result.returncode}]")

        return "\n".join(output_parts) if output_parts else "(命令执行成功，无输出)"

    except subprocess.TimeoutExpired:
        return f"[错误] 命令执行超时（30 秒上限）：{command}"
    except Exception as e:
        return f"[错误] 命令执行失败: {type(e).__name__}: {e}"

"""tools 包 — 工具注册表与工具实现

从 tools.py 拆分为包结构，每个工具独立文件。
集中式注册：所有注册调用在 create_default_registry() 中完成。
"""

from .registry import Tool, ToolRegistry
from .terminal import terminal_handler
from .read_file import read_file_handler


def create_default_registry() -> ToolRegistry:
    """工厂函数：创建并初始化一个包含默认工具的注册表。"""
    registry = ToolRegistry()

    # ---- terminal ----
    registry.register(
        name="terminal",
        description=(
            "在本地系统 shell 中执行命令，返回命令的输出结果。"
            "当你需要读取文件、运行脚本、安装软件、查询系统信息等操作时使用。"
            "对 Windows 使用 git-bash（类 Unix shell）环境。"
            "每条命令有 30 秒超时限制。"
            "注意：此命令会在用户系统上直接执行，请谨慎使用 rm、del 等破坏性操作。"
        ),
        parameters={
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令。使用 POSIX shell 语法（ls, cat, grep 等）。路径用斜杠（C:/Users/ 或 /c/Users/）。",
            }
        },
        handler=terminal_handler,
    )

    # ---- read_file ----
    registry.register(
        name="read_file",
        description=(
            "读取指定文件的内容并返回。"
            "当你需要查看代码、配置文件、日志等文本文件的内容时使用。"
            "支持 offset 和 limit 参数实现分页读取。"
        ),
        parameters={
            "path": {
                "type": "string",
                "description": "文件路径（绝对路径或相对路径）。",
            },
            "offset": {
                "type": "integer",
                "description": "起始行号（从 1 开始，默认 1）。",
            },
            "limit": {
                "type": "integer",
                "description": "最多读取的行数（默认全部）。",
            },
        },
        handler=read_file_handler,
    )

    return registry

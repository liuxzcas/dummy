"""tools 包 — 工具注册表与工具实现

从 tools.py 拆分为包结构，每个工具独立文件。
集中式注册：所有注册调用在 create_default_registry() 中完成。
"""

from .registry import Tool, ToolRegistry
from .terminal import terminal_handler
from .read_file import read_file_handler
from .write_file import write_file_handler
from .web_search import web_search_handler
from .web_extract import web_extract_handler


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

    # ---- write_file ----
    registry.register(
        name="write_file",
        description=(
            "将内容写入指定文件。"
            "当需要创建新文件或覆盖已有文件时使用。"
            "自动创建父目录。"
            "支持两种模式：overwrite（覆盖，默认）和 append（追加到文件末尾）。"
            "注意：只能写入文本内容（UTF-8 编码）。"
        ),
        parameters={
            "path": {
                "type": "string",
                "description": "文件路径。可以是绝对路径或相对于项目目录的路径。",
            },
            "content": {
                "type": "string",
                "description": "要写入的文件内容（文本）。",
            },
            "mode": {
                "type": "string",
                "description": "写入模式：overwrite（覆盖，默认）或 append（追加到文件末尾）。",
                "enum": ["overwrite", "append"],
            },
        },
        handler=write_file_handler,
    )

    # ---- web_search ----
    registry.register(
        name="web_search",
        description=(
            "搜索网络并返回搜索结果列表（标题、链接、摘要）。"
            "当需要查找实时信息、文档、新闻、代码示例等网络内容时使用。"
            "使用 DuckDuckGo 搜索引擎，无需 API Key。"
            "每条搜索请求有 15 秒超时。"
        ),
        parameters={
            "query": {
                "type": "string",
                "description": "搜索关键词。可以使用 site:domain 等过滤语法。",
            },
            "limit": {
                "type": "integer",
                "description": "返回的结果数量，默认 5，最大 10。",
            },
        },
        handler=web_search_handler,
    )

    # ---- web_extract ----
    registry.register(
        name="web_extract",
        description=(
            "提取指定 URL 网页的可读文本内容（去除广告、导航等噪音）。"
            "当需要读取在线文档、文章、API 文档等网页内容时使用。"
            "自动处理编码、提取标题。最大提取 10000 字符，超出部分截断。"
            "注意：无法渲染 JavaScript，SPA 页面可能提取不全。"
        ),
        parameters={
            "url": {
                "type": "string",
                "description": "网页 URL。自动补全 https:// 前缀。",
            },
            "char_limit": {
                "type": "integer",
                "description": "最大提取字符数，默认 10000。",
            },
        },
        handler=web_extract_handler,
    )

    return registry

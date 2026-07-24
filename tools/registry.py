"""
tools/registry.py — 工具注册表核心

定义了 Tool（单个工具的封装）和 ToolRegistry（注册表）两个类。
"""

from typing import Any, Callable


class Tool:
    """单个工具的封装。

    每个工具有三个核心要素：
    - name: LLM 通过这个名字来调用
    - schema: OpenAI 格式的工具定义（发给 LLM）
    - handler: 实际执行工具的函数
    """

    def __init__(self, name: str, schema: dict, handler: Callable[..., str]):
        self.name = name
        self.schema = schema
        self.handler = handler


class ToolRegistry:
    """工具注册表。

    核心职责：
    1. register():      注册新工具
    2. get_tool_definitions(): 获取所有工具定义（发给 LLM）
    3. dispatch():      根据工具名找到 handler 并执行
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., str],
    ):
        """注册一个新工具。"""
        schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": parameters,
                    "required": list(parameters.keys()),
                },
            },
        }
        self._tools[name] = Tool(name=name, schema=schema, handler=handler)

    def get_tool_definitions(self) -> list[dict]:
        """获取所有工具定义列表（发给 LLM 的 tools 参数）。"""
        return [tool.schema for tool in self._tools.values()]

    def dispatch(self, tool_name: str, arguments: dict) -> str:
        """根据工具名称和参数执行对应的 handler。"""
        tool = self._tools.get(tool_name)
        if tool is None:
            raise KeyError(
                f"未知工具 '{tool_name}'。可用工具: {list(self._tools.keys())}"
            )
        return tool.handler(**arguments)

    def list_tools(self) -> list[str]:
        """列出所有已注册的工具名称。"""
        return list(self._tools.keys())

"""
tools/registry.py — 工具注册表核心

定义了 Tool（单个工具的封装）和 ToolRegistry（注册表）两个类。

=== dispatch 层加固 ===

dispatch() 统一处理以下横切关注点，各 handler 只关心业务逻辑：

1. 异常兜底：所有 handler 抛出的异常都被捕获并格式化为错误字符串。
   这样 core.py 不需要为 dispatch 单独做 try/except。

2. 结果规范化：
   - None → ""
   - dict/list → json.dumps()
   - 其他类型 → str()
   确保回注给 LLM 的永远是字符串。

3. 超时控制：
   通过 tool.timeout 属性设置（默认无限制）。
   使用 concurrent.futures 实现跨平台超时。
"""

import json
from typing import Any, Callable, Optional


class Tool:
    """单个工具的封装。

    属性：
    - name: 工具名称，LLM 通过这个名字来调用
    - schema: OpenAI 格式的工具定义（发给 LLM）
    - handler: 实际执行工具的函数
    - timeout: 超时秒数（None=无限制，可用于防止 handler 卡死）
    - confirm: 是否需要用户确认（确认由 core 统一管理，工具自身不接触交互）
    """

    def __init__(
        self,
        name: str,
        schema: dict,
        handler: Callable[..., str],
        timeout: Optional[int] = None,
        confirm: bool = False,
    ):
        self.name = name
        self.schema = schema
        self.handler = handler
        self.timeout = timeout
        self.confirm = confirm


class ToolRegistry:
    """工具注册表。

    核心职责：
    1. register():      注册新工具
    2. get_tool_definitions(): 获取所有工具定义（发给 LLM）
    3. dispatch():      根据工具名找到 handler 并执行（含加固逻辑）
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., str],
        timeout: Optional[int] = None,
        confirm: bool = False,
    ):
        """注册一个新工具。

        新增参数：
        - timeout: 可选，该工具的超时秒数。
                   超过此时间未返回会被强制中断。
        - confirm: 可选，是否需要用户确认。确认 UI 由 core 统一管理
                   (dispatch 前检查 requires_confirm),handler 保持纯函数。
        """
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
        self._tools[name] = Tool(
            name=name,
            schema=schema,
            handler=handler,
            timeout=timeout,
            confirm=confirm,
        )

    def requires_confirm(self, name: str) -> bool:
        """该工具是否需要用户确认(core 在 dispatch 前调用)。"""
        tool = self._tools.get(name)
        return bool(tool and tool.confirm)

    def get_tool_definitions(self) -> list[dict]:
        """获取所有工具定义列表（发给 LLM 的 tools 参数）。"""
        return [tool.schema for tool in self._tools.values()]

    def _normalize_result(self, raw: Any) -> str:
        """将任意类型的工具返回值规范化为字符串。

        规则：
        - None → ""
        - str → 原样返回
        - list/dict → json.dumps()
        - 其他 → str()
        """
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        if isinstance(raw, (list, dict)):
            try:
                return json.dumps(raw, ensure_ascii=False)
            except Exception:
                return str(raw)
        return str(raw)

    def dispatch(self, tool_name: str, arguments: dict) -> str:
        """根据工具名称和参数执行对应的 handler。

        === 加固逻辑 ===

        1. 查找工具（不存在时返回错误字符串，不抛异常）
        2. 调用 handler（带超时控制，如果配置了）
        3. 异常兜底（任何异常都被捕获并格式化为错误字符串）
        4. 结果规范化（保证始终返回字符串）

        这样 core.py 调用 dispatch 时不需要额外的 try/except。
        """

        # ---- 1. 查找工具 ----
        tool = self._tools.get(tool_name)
        if tool is None:
            return (
                f"[ToolDispatch] 未知工具: '{tool_name}'。"
                f" 可用工具: {list(self._tools.keys())}"
            )

        # ---- 2. 参数校验 ----
        # 如果 core.py 解析 JSON 失败，会把错误信息放在 _error 中
        # 此时不调用 handler，直接返回错误
        if "_error" in arguments:
            return f"[ToolDispatch] {tool.name} 参数错误: {arguments['_error']}"

        # ---- 3. 执行 handler ----
        try:
            if tool.timeout is not None:
                # 带超时执行
                result = self._run_with_timeout(tool, arguments)
            else:
                result = tool.handler(**arguments)

        except Exception as e:
            return f"[ToolDispatch] {tool.name} 执行异常: {type(e).__name__}: {e}"

        # ---- 4. 结果规范化 ----
        return self._normalize_result(result)

    def _run_with_timeout(self, tool: Tool, arguments: dict) -> str:
        """在子线程中执行 handler，超时则中断。"""
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(tool.handler, **arguments)
            try:
                return future.result(timeout=tool.timeout)
            except concurrent.futures.TimeoutError:
                raise TimeoutError(
                    f"工具 '{tool.name}' 执行超时（{tool.timeout} 秒）"
                )

    def list_tools(self) -> list[str]:
        """列出所有已注册的工具名称。"""
        return list(self._tools.keys())

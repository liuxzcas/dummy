"""
tools/registry.py — 工具注册表核心

定义了 Tool（单个工具的封装）和 ToolRegistry（注册表）两个类。

=== dispatch 层加固 ===

dispatch() 统一处理以下横切关注点，各 handler 只关心业务逻辑：

1. 异常兜底：所有 handler 抛出的异常都被捕获并格式化为错误字符串。
   这样 core.py 不需要为 dispatch 单独做 try/except。
   —— 例外：InterruptSignal（用户 /p 打断）必须穿透，不做兜底。

2. 结果规范化：
   - None → ""
   - dict/list → json.dumps()
   - 其他类型 → str()
   确保回注给 LLM 的永远是字符串。

3. 超时控制：
   通过 tool.timeout 属性设置（默认无限制）。
   使用 concurrent.futures 实现跨平台超时。

=== 确认机制（2026-08-14 定稿） ===

确认语义归 handler（每种工具展示自己的确认内容），/p 拦截归 core：

- 工具注册时 confirm=True 声明需要确认
- dispatch 时向 handler 注入 _confirm 函数（core 提供）：
  * 输入收集：所有确认输入统一经 _confirm（单点 /p 检查）
  * /p 拦截：命中 '/p' 抛 InterruptSignal（纯触发，不携带提示词），
    普通输入原样返回给 handler 做 y/n/d 语义判断
- handler 不裸调 input()，打断不可能被 handler 内部逻辑误用
"""

import json
from typing import Any, Callable, Optional


class InterruptSignal(Exception):
    """用户 /p 打断信号（纯触发，不携带提示词）。

    由 core 注入的 confirm 函数在用户输入 '/p' 时抛出，穿透 dispatch
    （不做异常兜底），由 chat() 循环捕获处理。提示词在打断处理点
    统一收集（阻塞式 input），不在信号里传递。
    """

    def __init__(self):
        super().__init__("用户 /p 打断")


class Tool:
    """单个工具的封装。

    属性：
    - name: 工具名称，LLM 通过这个名字来调用
    - schema: OpenAI 格式的工具定义（发给 LLM）
    - handler: 实际执行工具的函数
    - timeout: 超时秒数（None=无限制，可用于防止 handler 卡死）
    - confirm: 是否需要用户确认（确认交互在 handler 内，但输入收集与
               /p 拦截由 core 注入的 _confirm 函数统一处理）
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
        # 确认函数提供者（core 注入）：handler 的 _confirm 参数从这里来
        self._confirm_provider: Optional[Callable[[str], str]] = None

    def set_confirm_provider(self, provider: Callable[[str], str]) -> None:
        """注入确认函数提供者（core 在构造后调用）。

        provider 是一个包装 input() 的函数：内部先做 '/p' 检查，
        命中抛 InterruptSignal，否则把用户输入原样返回给 handler 处理。
        """
        self._confirm_provider = provider

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
        - confirm: 可选，是否需要用户确认。确认交互写在 handler 内
                   （通过可选的 _confirm 参数），输入收集与 /p 拦截由
                   core 注入的确认函数统一处理。
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

    def dispatch(self, tool_name: str, arguments: dict) -> str:
        """根据工具名称和参数执行对应的 handler。

        === 加固逻辑 ===

        1. 查找工具（不存在时返回错误字符串，不抛异常）
        2. 调用 handler（带超时控制，如果配置了）
        3. 异常兜底（任何异常都被捕获并格式化为错误字符串）
           —— InterruptSignal 除外：必须穿透，让 chat 循环处理打断
        4. 结果规范化（保证始终返回字符串）

        这样 core.py 调用 dispatch 时不需要额外的 try/except
        （InterruptSignal 例外，core 显式捕获）。
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
        # 确认类工具：注入 _confirm 函数（输入收集 + /p 拦截在 core）
        if tool.confirm and self._confirm_provider is not None:
            arguments["_confirm"] = self._confirm_provider
        try:
            if tool.timeout is not None:
                # 带超时执行
                result = self._run_with_timeout(tool, arguments)
            else:
                result = tool.handler(**arguments)

        except InterruptSignal:
            # 用户 /p 打断：必须穿透，不做异常兜底
            # （否则打断会变成一条"执行异常"字符串回注给 LLM）
            raise
        except Exception as e:
            return f"[ToolDispatch] {tool.name} 执行异常: {type(e).__name__}: {e}"

        # ---- 4. 结果规范化 ----
        return self._normalize_result(result)

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

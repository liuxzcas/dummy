"""
============================================================
tools.py — 工具注册表与工具实现
============================================================

本模块实现了两个关键部分：
1. ToolRegistry 类 —— 工具的注册与发现中心
2. 具体的工具实现（Phase 0 只有一个：terminal）

=== 为什么需要工具注册表？===

直观的做法是写一个 if-else 大函数：
  if tool_name == "terminal":
      run_terminal(args)
  elif tool_name == "search":
      run_search(args)
  ...

这在小规模下可以工作，但有几个问题：
1. 每加一个工具就要修改这个中心函数（违反开闭原则）
2. 无法在运行时动态查询有哪些工具可用
3. 工具定义（给 LLM 看的 JSON Schema）和工具实现分散在不同地方

注册表模式解决了这些问题：
- 每个工具注册时同时提交：name、description、parameters schema、handler
- 注册表可以提供统一查询接口（给 core.py 用）
- 新工具只是一个新的注册调用，不修改现有代码

=== JSON Schema 是什么？为什么 LLM 需要它？===

JSON Schema 是一种描述 JSON 数据格式的标准。
这里我们用它描述工具的输入参数。

例：terminal 工具接受一个 command 参数（字符串）

LLM 本身不知道 terminal 是什么。
你给它的 tools 参数中的"描述"和"参数定义"，就是它理解工具的唯一途径。
这就是为什么 description 要写清楚、parameters 要定义精确——
写不好，LLM 就不会正确调用。

=== 技术决策 ===

方案 A（选定）：集中式注册表 + 函数实现
    优点：清晰、可扩展、易于测试
    缺点：每个工具需要显式注册

方案 B：装饰器模式（@tool.register）
    优点：更"Pythonic"、注册和定义在一起
    缺点：装饰器对初学者不够直观

方案 C：从类型注解自动生成 Schema
    优点：减少重复
    缺点：依赖类型系统、不够灵活

Phase 0 选择方案 A，因为：
1. 最直观，每行代码在做什么一目了然
2. 适合学习（注册表模式在很多系统中都有应用）
3. 后续可以重构成装饰器模式（一次一步）
============================================================
"""

import json
import subprocess
import sys
from typing import Any, Callable, Optional


# ===========================================================
# 工具定义的数据结构
# ===========================================================
# 每个工具有三个核心要素：
# 1. name — LLM 调用时使用的名字（必须唯一）
# 2. schema — JSON Schema 格式的定义（发给 LLM 的）
# 3. handler — 实际执行的 Python 函数
# ===========================================================


class Tool:
    """
    单个工具的封装。

    属性：
    - name: 工具名称，LLM 通过这个名字来调用
    - schema: OpenAI 格式的工具定义（发给 LLM）
    - handler: 实际执行工具的函数
              签名：def handler(**kwargs) -> str
              返回值必须是字符串（回注给 LLM 时要用）
    """

    def __init__(self, name: str, schema: dict, handler: Callable[..., str]):
        self.name = name
        self.schema = schema
        self.handler = handler


class ToolRegistry:
    """
    工具注册表。

    核心职责：
    1. register(): 注册新工具
    2. get_tool_definitions(): 获取所有工具定义（发给 LLM）
    3. dispatch(): 根据 LLM 返回的 tool_call 找到对应 handler 并执行

    === 数据流 ===
    Agent Loop → dispatch(tool_call) → Registry 查找 name →
    找到 Tool 对象 → handler(**arguments) → 返回结果字符串
    """

    def __init__(self):
        # 用字典存储工具，key 是 name，O(1) 查找
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: Callable[..., str],
    ):
        """
        注册一个新工具。

        参数：
        - name: 工具名称。对 LLM 调用来讲就是函数名。
                建议 snake_case，因为 LLM 训练数据中多用这种风格。
        - description: 工具功能的自然语言描述。
                       **这是最重要的字段。**
                       LLM 靠这个描述来决定"什么时候调用这个工具"。
                       写清楚：工具做什么、什么时候应该用、什么时候不该用。
        - parameters: JSON Schema 对象，描述工具的参数结构。
                      标准格式：{ "type": "object", "properties": {...}, "required": [...] }
                      每个 property 也要写 description，LLM 靠它理解参数含义。
        - handler: 实际执行的函数。接收 keyword arguments，返回字符串。

        === 为什么 handler 必须返回字符串？===
        因为 LLM 只看文本。返回的内容会以 tool role 消息回注给 LLM。
        如果返回 dict 或其它类型，需要序列化。
        统一要求字符串避免调用方每次都做 str()。

        === 注意：description 和 parameters 中的 description 的区别 ===
        - 顶层的 description：描述"什么时候调用这个工具"
        - 参数的 description：描述"这个参数应该传什么值"
        - 两者都是 LLM 做决策的依据
        """
        # -------------------------------------------------------
        # 构建 OpenAI API 标准的 tool 定义
        # 参考：https://platform.openai.com/docs/guides/function-calling
        # -------------------------------------------------------
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

        tool = Tool(name=name, schema=schema, handler=handler)
        self._tools[name] = tool

    def get_tool_definitions(self) -> list[dict]:
        """
        获取所有工具定义列表。
        这个列表直接传给 LLM 的 tools 参数。
        """
        return [tool.schema for tool in self._tools.values()]

    def dispatch(self, tool_name: str, arguments: dict) -> str:
        """
        根据工具名称和参数执行对应的 handler。

        参数：
        - tool_name: LLM 返回的 tool_calls[].function.name
        - arguments: 解析后的参数字典（LLM 返回的是 JSON 字符串，需要先解析）

        返回值：
        工具执行结果的字符串。

        异常：
        - KeyError: 工具不存在
        - TypeError: 参数匹配问题
        - 各种执行时异常（取决于具体工具）

        Phase 0 不做容错 —— 让异常向上传播，
        方便调试时看到完整的 traceback。
        """
        tool = self._tools.get(tool_name)
        if tool is None:
            raise KeyError(
                f"未知工具 '{tool_name}'。可用工具: {list(self._tools.keys())}"
            )

        # 调用 handler，传入解析后的参数
        # **arguments 将 dict 解包为 keyword arguments
        # 例如 arguments = {"command": "ls -la"}
        # 相当于 handler(command="ls -la")
        return tool.handler(**arguments)

    def list_tools(self) -> list[str]:
        """列出所有已注册的工具名称。"""
        return list(self._tools.keys())


# ===========================================================
# 工具实现
# ===========================================================
# 这里定义具体的工具函数，然后通过注册表注册。
# 每个工具独立为一个函数，便于测试和复用。


def terminal_handler(command: str) -> str:
    """
    terminal 工具的具体实现。

    功能：在本地 shell 中执行指定的命令，返回输出。

    参数：
    - command: 要执行的 shell 命令字符串。

    返回值：
    包含以下内容的字符串：
    - stdout（命令的标准输出）
    - stderr（如果有的话）
    - 退出码（如果非零）

    === 安全说明 ===
    shell=True 意味着字符串直接传给系统 shell 解释执行。
    这非常强大但也非常危险。
    想象一下 LLM 被 prompt injection 攻击后执行 "rm -rf /"……

    安全缓解措施（后续 Phase 加入）：
    1. 命令批准机制（用户确认后才执行）
    2. 危险命令黑名单
    3. 沙箱执行环境

    对于 Phase 0 学习项目，我们接受这个风险，
    但必须在注释中明确警告。

    === subprocess.run 说明 ===
    - shell=True: 通过系统 shell 执行（Windows 上是 cmd.exe，git-bash 下是 bash）
    - capture_output=True: 捕获 stdout 和 stderr
    - text=True: 以文本模式（而非 bytes）返回输出
    - timeout=30: 30 秒超时，防止命令永远挂起
    """
    try:
        # -------------------------------------------------------
        # subprocess.run 会阻塞等待命令完成。
        # 对于长时间运行的命令（如服务器），这个函数会等 30 秒然后超时。
        # 后续 Phase 可以改为异步执行。
        # -------------------------------------------------------
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

        # -------------------------------------------------------
        # 构建输出字符串
        # 格式约定：
        # 1. 先 stdout
        # 2. 如果有 stderr，附加 [STDERR] 标记后输出
        # 3. 如果退出码非零，附加退出码
        #
        # 为什么保留退出码？因为 LLM 需要知道命令是否成功执行。
        # 退出码 0 = 成功，非 0 = 失败。
        # LLM 可以根据退出码决定下一步行动（如重试或报告错误）。
        # -------------------------------------------------------
        output_parts = []

        # stdout 可能为空（如 rm 命令不输出任何东西）
        stdout = result.stdout.strip()
        if stdout:
            output_parts.append(stdout)

        stderr = result.stderr.strip()
        if stderr:
            output_parts.append(f"[STDERR]\n{stderr}")

        # 只有退出码非零时才显式标记
        if result.returncode != 0:
            output_parts.append(f"[EXIT CODE: {result.returncode}]")

        return "\n".join(output_parts) if output_parts else "(命令执行成功，无输出)"

    except subprocess.TimeoutExpired:
        return f"[错误] 命令执行超时（30 秒上限）：{command}"
    except Exception as e:
        return f"[错误] 命令执行失败: {type(e).__name__}: {e}"


def create_default_registry() -> ToolRegistry:
    """
    工厂函数：创建并初始化一个包含默认工具的注册表。

    为什么用工厂函数而不是在模块级别执行？
    1. 模块级代码在 import 时执行，难以控制执行顺序
    2. 工厂函数可测试：每次调用得到全新的注册表
    3. 后续可以有不同的"配置"（如安全模式禁用某些工具）

    这个函数是 core.py 和 main.py 获取工具的入口。
    """
    registry = ToolRegistry()

    # -------------------------------------------------------
    # 注册 terminal 工具
    # -------------------------------------------------------
    # name: "terminal" — LLM 通过这个名字调用
    # description: 详细说明工具的用途和使用场景
    # parameters: 定义命令参数的结构
    # handler: 具体的实现函数
    # -------------------------------------------------------
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

    return registry

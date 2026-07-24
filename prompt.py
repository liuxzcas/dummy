"""
============================================================
prompt.py — System Prompt 构造器
============================================================

=== System Prompt 在 Agent 中的作用 ===

System Prompt（系统提示词）是发给 LLM 的第一条消息，
role="system"。它设定了 LLM 在整个对话中的行为基准。

相比于 user message，system message 有更高的"优先级"——
大多数 LLM 的训练数据会让 model 更重视 system 中的指令。

一个有效的 system prompt 通常包含：
1. 身份定义：我是谁、我能做什么
2. 能力边界：我有什么工具、什么情况下使用
3. 行为规则：我应该遵循的决策逻辑
4. 输出格式：我该如何组织回答

=== 为什么不在 core.py 里硬编码提示词？===

把提示词抽成独立模块有三个好处：
1. 便于迭代 —— 改提示词不需要动核心逻辑
2. 多语言支持 —— 可以给英文用户不同的提示词
3. 可测试性 —— 可以写测试验证提示词的质量

=== 设计决策 ===

方案 A（选定）：使用 f-string 将工具描述注入提示词
    优点：灵活，添加新工具时自动反映在提示词中
    缺点：提示词文本分散在代码中

方案 B：从 YAML/JSON 文件加载提示词模板
    优点：非技术人员也能改提示词
    缺点：对 Phase 0 过度设计

方案 C：LangChain 的 PromptTemplate
    优点：功能强大
    缺点：引入不必要的依赖

选择方案 A，因为 Phase 0 的提示词很少，
硬编码 + f-string 是最直接的方式。
随着项目增长，可以重构为模板文件方案。
============================================================
"""
import datetime
import sys


# ===========================================================
# 系统提示词模板
# ===========================================================
# 注意：{tool_descriptions} 是占位符，会被 build_system_prompt()
# 替换为实际的工具描述文本。
#
# 为什么要把工具描述写两遍？
# 一次在 System Prompt 的文本里（这里的 {tool_descriptions}），
# 一次在 API 的 tools 参数里（tools.py 中的 schema）。
#
# 这是因为：
# 1. API 的 tools 参数是结构化的 schema，LLM 用来精确构建调用
# 2. System Prompt 的文本描述是自然语言，LLM 用来理解"什么时候"用
# 两者缺一不可。
# ===========================================================

SYSTEM_PROMPT_TEMPLATE = """你是 Dummy Agent，一个正在学习工具调用的 AI 助手。

## 可用工具

以下工具你可以调用。当你需要执行操作时，使用相应的工具：
{tool_descriptions}

## 使用规则

1. 当你需要执行系统操作（查看文件、运行命令等）时：
   - 先思考需要哪个工具
   - 调用工具
   - 根据工具返回的结果继续处理

2. 当用户只是提问或聊天时，直接回答即可。

3. 工具可能返回错误信息。如果出错，尝试换个方式重试，
   或告诉用户为什么失败了。

4. 如果你需要多个步骤才能完成用户的要求，
   请一步一步来：调一个工具 → 看结果 → 再调下一个。

## 环境信息

- 操作系统: {os_info}
- 运行时间: {timestamp}"""


def build_system_prompt(tool_names: list[str]) -> str:
    """
    构建完整的 System Prompt。

    参数：
    - tool_names: 可用工具的名称列表（用于在提示词中列举）

    返回值：
    填充好的 system prompt 字符串。

    === 为什么用函数而不是直接导出字符串？===
    因为需要在运行时动态注入系统环境信息
    （操作系统、时间等），这些在 import 时是不知道的。
    """
    # 构建工具描述列表
    # 格式：
    # - terminal: 在本地 shell 中执行命令
    tool_lines = []
    for name in tool_names:
        if name == "terminal":
            tool_lines.append(f"- {name}: 执行 shell 命令。支持 ls、cat、python 等。")
        else:
            tool_lines.append(f"- {name}")

    tool_descriptions = "\n".join(tool_lines)

    # 环境信息
    os_info = f"{sys.platform} (Windows via git-bash)"

    # 当前时间，让 LLM 知道对话发生的时间上下文
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z")

    return SYSTEM_PROMPT_TEMPLATE.format(
        tool_descriptions=tool_descriptions,
        os_info=os_info,
        timestamp=timestamp,
    )

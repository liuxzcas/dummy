"""
============================================================
llm.py — LLM API 封装模块
============================================================

本模块封装了对 OpenAI 兼容格式 LLM API 的调用(默认 DeepSeek,
base_url/api_key/model 均可配置,任意兼容服务可接入),
重点关注 Chat Completions 接口中的 Tool/Function Calling 功能。

=== 背景知识：什么是 OpenAI 兼容 API？===

OpenAI 定义了 Chat Completions API 的事实标准：
  POST {base_url}/chat/completions
  Body: { model, messages, tools, ... }
  Response: { choices: [{ message: { content, tool_calls } }] }

DeepSeek、Qwen、GLM、vLLM 等数十个提供商的 API 都兼容此格式。
这意味着用同一套代码可以切换任何兼容 provider ——
只需要改 base_url、api_key 和 model 名字。

=== Tool Calling 的核心概念 ===

1. tool 定义（发送给 LLM）
   LLM 本身不知道你的系统里有哪些功能可用。
   你需要通过 tools 参数把每个工具的 JSON Schema 发给它。
   Schema 包含：name、description、parameters（标准 JSON Schema）。
   LLM 根据这些描述"决定"调用哪个工具、传什么参数。

2. tool_calls（LLM 返回）
   当 LLM 认为应该调用工具时，它在 response 中返回 tool_calls 数组：
   [
     {
       "id": "call_xxx",          // 唯一标识，用于关联结果
       "type": "function",
       "function": {
         "name": "terminal",
         "arguments": "{\"command\": \"ls\"}"  // JSON 字符串
       }
     }
   ]

3. tool 结果回注（你发给 LLM）
   你不能直接说"命令执行完了"——
   你必须用 tool role 消息把结果精确回注：
   {
     "role": "tool",
     "tool_call_id": "call_xxx",   // 和 tool_calls 的 id 对应
     "content": "ls 的输出内容"
   }
   然后 LLM 看到结果后，决定继续调用工具还是生成最终回答。

=== 为什么用 openai Python SDK 而不是直接发 HTTP？===

直接用 requests 也是可以的（很多学习项目这么做），但这需要：
- 自己处理 JSON 序列化
- 自己处理流式响应
- 自己处理认证头
- 自己处理错误重试

对于学习 Phase 0，SDK 让你聚焦在 agent 逻辑本身。
在注释里我会同时指出底层发生了什么。
============================================================
"""

import json
from typing import Optional

# ----------------------------
# openai 库的导入
# OpenAI 库 v1.0+ 使用新的客户端 API，
# 支持同步和异步两种调用方式。
# 我们 Phase 0 用同步调用，更简单直观。
# ----------------------------
from openai import OpenAI


class LLMClient:
    """
    LLM 客户端封装类。

    === 设计决策 ===
    为什么封装成类而不是函数？
    1. 状态管理：client 实例包含了 api_key、base_url 等配置，
       封装成类避免这些参数在函数间传来传去。
    2. 可扩展：后续可以添加 model 切换、retry 逻辑、token 统计等，
       类的内部改动不影响外部调用接口。
    3. 可测试：可以 mock 这个类来测试 agent 逻辑。

    === 备选方案分析 ===
    方案 A（选定）：封装类 + OpenAI SDK
       优点：代码清晰、SDK 处理了协议细节、易于扩展
       缺点：多了一层抽象、引入了依赖

    方案 B：直接用 requests 发 HTTP
       优点：零依赖、完全透明
       缺点：需要自己处理协议细节、容易出错

    方案 C：用 LangChain 等框架
       优点：高级抽象、自带很多功能
       缺点：学习成本高、框架限制了灵活性
       （对于学习项目来说，坏处大于好处）

    选定方案 A，因为 Phase 0 的目标是学核心概念，
    而不是学 HTTP 协议实现。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
    ):
        """
        初始化 LLM 客户端(OpenAI 兼容 API)。
 
        参数说明：
        - api_key: API 密钥。任意 OpenAI 兼容服务均可——
                   DeepSeek: https://platform.deepseek.com/api_keys
                   OpenAI / Ollama 本地 / vLLM 等按其服务获取。
                   注意：密钥应当通过环境变量传入，而不是硬编码在代码里。
        - base_url: API 基础地址。指向任意 OpenAI 兼容端点：
                   DeepSeek 官方: https://api.deepseek.com
                   Ollama 本地: http://localhost:11434/v1
                   其他 provider 改这个即可，无需改代码。
        - model: 模型名称。按服务提供方的模型名填写：
                 DeepSeek-V3: "deepseek-chat"
                 OpenAI: "gpt-4o" 等
                 Ollama: "qwen2.5:7b" 等
        """
        # -------------------------------------------------------
        # OpenAI 客户端初始化
        # 底层做的事情：
        # 1. 验证 api_key 不为空
        # 2. 拼接 base_url + "/chat/completions" 作为实际请求地址
        # 3. 创建 HTTP 会话（默认使用 httpx）
        # 4. 设置请求头：Authorization: Bearer {api_key}
        # -------------------------------------------------------
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

        # 保存基础信息，后续需要打印或调试时使用
        self.base_url = base_url

        # -------------------------------------------------------
        # last_usage: 最近一次 API 调用返回的 usage(含 prompt_tokens)。
        # Phase 2.2 上下文压缩用它做精确 token 计数——
        # usage.prompt_tokens 就是模型这次实际看到的上下文大小
        # (含 system prompt + 工具定义),比任何字符估算都可靠。
        # 首次调用前为 None,should_compress 会跳过。
        # -------------------------------------------------------
        self.last_usage = None

        # last_reasoning: 最近一次响应的思考内容(deepseek-reasoner /
        # deepseek-chat thinking 模式返回 reasoning_content)。
        # 供上层显示;不入 history(API 拒收/浪费 token)。
        self.last_reasoning = None

    def chat(
        self,
        messages: list,
        tools: Optional[list] = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
    ):
        """
        发送 Chat Completion 请求，可选携带 tool 定义。

        参数：
        - messages: OpenAI 格式的消息列表。
          [
            {"role": "system", "content": "..."},
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": "...", "tool_calls": [...]},
            {"role": "tool", "tool_call_id": "...", "content": "..."},
          ]
          注意 role 必须严格交替：user → assistant → tool → assistant → user → ...
          违反交替规则会导致某些模型出错。

        - tools: JSON Schema 格式的工具定义列表（可选）。
          每个工具定义的格式：
          {
            "type": "function",
            "function": {
              "name": "tool_name",
              "description": "工具描述",
              "parameters": { ... }  // JSON Schema object
            }
          }

        - temperature: 生成温度（0.0~2.0）。
          较低值（如 0.1）输出更确定，适合工具调用。
          较高值（如 0.8）输出更有创造力。
          对于 agent 场景，推荐 0.1~0.3，减少幻觉。

        - max_tokens: 最大生成 token 数。
          注意：这包括 tool_calls 产生的 token。
          如果设太小，LLM 可能在生成完整的 tool_calls 之前被截断。

        返回值：
          OpenAI 的 chat.completions 响应对象。
          最关键的两个字段：
          - response.choices[0].message.content: 文本回答（如果有）
          - response.choices[0].message.tool_calls: 工具调用（如果有）

        === 异常处理 ===
        常见的 API 错误：
        - AuthenticationError: API Key 无效
        - RateLimitError: 请求频率超限（DeepSeek 免费用户有 3 RPM 限制）
        - BadRequestError: 请求格式错误（通常是 messages 格式不对）
        - Timeout: 网络超时

        Phase 0 先不做自动重试，出错直接抛出，
        因为出现错误说明配置或代码有问题，需要人工介入。
        """
        # -------------------------------------------------------
        # 构建请求参数（SDK 内部会序列化为 JSON 发送 POST 请求）
        # 底层的 HTTP 请求体大概长这样：
        # {
        #   "model": "deepseek-chat",
        #   "messages": [...],
        #   "tools": [...] (如果提供了),
        #   "temperature": 0.7,
        #   "max_tokens": 4096
        # }
        # -------------------------------------------------------
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # 只有确实有工具时才传 tools 参数
        # 如果传空列表 []，某些 API 会报错
        if tools:
            kwargs["tools"] = tools

        # -------------------------------------------------------
        # 调用 API
        # 这里会实际发出 HTTP 请求，所以可能耗时。
        # SDK 默认超时 60 秒，可以通过 timeout 参数调整。
        # -------------------------------------------------------
        response = self.client.chat.completions.create(**kwargs)

        # -------------------------------------------------------
        # 记录本次调用的 usage(token 计数),供上下文压缩做触发判断。
        # getattr 防御:极少数 provider / 流式场景可能不返回 usage,
        # 此时 last_usage 为 None,压缩逻辑会跳过(安全降级)。
        # -------------------------------------------------------
        self.last_usage = getattr(response, "usage", None)

        # 提取思考内容(非标准字段,SDK 可能放 model_extra):
        # reasoning_content 是 DeepSeek 推理模型在最终回答前的思维链。
        message = response.choices[0].message
        self.last_reasoning = getattr(message, "reasoning_content", None)
        if self.last_reasoning is None:
            extra = getattr(message, "model_extra", None) or {}
            self.last_reasoning = extra.get("reasoning_content")

        return message


    def get_model_name(self) -> str:
        """返回当前使用的模型名称（用于日志显示）。"""
        return self.model


def extract_cached_tokens(usage) -> int:
    """从 usage 中提取缓存命中 token 数(双字段兼容)。

    DeepSeek 系返回两种形态:
    - prompt_tokens_details.cached_tokens(标准字段)
    - prompt_cache_hit_tokens(兼容字段)
    都缺失时返回 0(provider 未统计缓存)。
    """
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", None)
        if cached is not None:
            return int(cached)
    cached = getattr(usage, "prompt_cache_hit_tokens", None)
    return int(cached) if cached else 0


# 默认上下文窗口(全局常量,compressor 与 llm 共用;按主模型调整)。
# 主模型 deepseek-v4-flash 原生窗口 1M,V4 论文确认 128K 内检索性能
# 稳定,取 128K 作为压缩阈值基准(压缩过早浪费上下文,过晚成本高)。
DEFAULT_WINDOW_TOKENS = 128000

# 常见本地/第三方部署模型的上下文窗口(token)。
# 匹配不上时用 DEFAULT_WINDOW_TOKENS(compressor 的 CompressionConfig.window_tokens)。
# 作用:本地模型窗口各异,窗口占用比例与压缩阈值需按模型解析。
MODEL_WINDOWS = {
    "qwen2.5:0.5b": 32768,
    "qwen2.5:1.5b": 32768,
    "qwen2.5:3b": 32768,
    "qwen2.5:7b": 32768,
    "qwen2.5:14b": 32768,
    "qwen2.5:32b": 32768,
    "qwen2.5:72b": 32768,
    "llama3": 8192,
    "llama3.1": 131072,
    "llama3.2": 131072,
    "llama3.3": 131072,
    "deepseek-r1:7b": 65536,
    "deepseek-r1:14b": 65536,
    "deepseek-r1:32b": 65536,
    "deepseek-r1:70b": 65536,
    "qwen3:8b": 32768,
    "qwen3:32b": 32768,
}


def resolve_window_tokens(model: str | None, default: int = DEFAULT_WINDOW_TOKENS) -> int:
    """按模型名解析上下文窗口;匹配不上返回默认值。"""
    if not model:
        return default
    return MODEL_WINDOWS.get(model, default)


def is_local_endpoint(base_url: str | None) -> bool:
    """判断是否为本地部署端点(ollama/vllm 等)。

    本地部署无 API 计费,成本统计应显示"本地"而非按云端单价估算。
    """
    if not base_url:
        return False
    return any(
        h in base_url for h in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
    )

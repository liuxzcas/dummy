"""
============================================================
core.py — Agent 核心循环
============================================================

本模块是整个项目的核心 —— 实现了 Tool-Calling Agent 的主循环。

=== Agent 是什么？===

在 LLM 的语境下，Agent = LLM + Tools + Loop。

- LLM 提供"大脑"：理解用户意图、做出决策
- Tools 提供"手脚"：执行具体操作（运行命令、读文件等）
- Loop 提供"控制流"：LLM 决定做什么 → 执行 → 看结果 → 再决定

=== 核心循环：Tool-Calling Loop ===

这是 Agent 最基础的结构，所有复杂 Agent 系统
（AutoGPT、LangChain Agent、Claude Code、Hermes Agent）都基于此。

流程：
                          ┌──────────────┐
                          │  用户输入     │
                          └──────┬───────┘
                                 ▼
                    ┌──────────────────────┐
                    │ 构造消息列表          │
                    │ (system + 历史 + 新消息)│
                    └──────────┬───────────┘
                               ▼
                    ┌──────────────────────┐
                    │ 调用 LLM（带工具定义） │
                    └──────────┬───────────┘
                               ▼
                    ┌──────────────────────┐
                    │  LLM 返回了什么？     │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
        ┌──────────┐    ┌──────────┐    ┌──────────┐
        │ 文本回答 │    │ tool_call│    │ 其它/空  │
        └────┬─────┘    └────┬─────┘    └────┬─────┘
             │               │               │
             ▼               ▼               ▼
        ┌──────────┐  ┌──────────────┐ ┌──────────┐
        │ 返回给   │  │ 执行工具     │ │ 错误处理 │
        │ 用户     │  │ 回注结果     │ └──────────┘
        └──────────┘  │ 回到循环    │
                      └──────┬───────┘
                             │
                             ▼
                    ┌──────────────────────┐
                    │  再次调用 LLM        │
                    └──────────────────────┘

=== 为什么不能只用一次 LLM 调用？===

因为 LLM 不知道工具执行的结果。
比如用户说"当前目录有什么文件"，LLM 调用 terminal("ls")。
它不知道 ls 输出了什么，必须等执行结果回来才能回答用户。
所以需要循环：先调工具 → 拿到结果 → 再调 LLM 生成最终回答。

这个过程可能有多步：
用户："安装 Flask 并启动服务器"
Agent：terminal("pip install flask") → LLM 看到成功
      → terminal("flask run") → LLM 看到输出
      → 告诉用户"服务器已在 http://127.0.0.1:5000 运行"

=== Phase 0 的限制 ===

1. 无状态保存：退出后历史丢失
2. 无 context compression：长对话会超 token 限制
3. 无并行 tool calling：一次只处理一个工具调用
4. 无安全审批：工具直接执行

这些在后续 Phase 逐步解决。
============================================================
"""

import json
import os
import re
import datetime
import threading
import time
from typing import Optional

from llm import LLMClient
from memory import MemoryExtractor
from tools import ToolRegistry
from tools.registry import InterruptSignal
from prompt import build_system_prompt
from session_store import SessionStore
from compressor import (
    ContextCompressor,
    CompressionConfig,
    CompressionResult,
    strip_meta,
)


# ===========================================================
# Agent 类
# ===========================================================
# 封装了 agent 的所有状态：LLM 客户端、工具集、对话历史、配置。
#
# 为什么不设计成纯函数？
# 对话历史需要累积，状态需要保持。
# 类是最自然的方式。后续可以加数据库持久化。=================


class _InterruptListener:
    """随时打断监听器(后台线程,无回显)。

    工具运行中/LLM 等待时,用户随时敲 "/p + Enter" 触发打断:
    - 后台线程持续轮询 msvcrt.kbhit(),字符累积到 buffer(无回显,
      用户输入不可见,像密码输入),完整行以换行标记结束
    - chat() 循环检查点 take_line() 消费完整行,在下一轮工具调用
      前作出反应
    - pause()/resume():确认 input() 期间暂停轮询,避免与 input
      竞争 stdin(输入由 input 统一收集,仍走 /p 拦截)
    非 Windows / 非 console 环境自动退化(take_line 恒返回 None)。
    """

    def __init__(self):
        self._buffer = ""
        self._paused = False
        self._stop = False
        self._lock = threading.Lock()  # 保护 buffer(线程写/检查点读)
        self._msvcrt = None
        try:
            import msvcrt  # Windows console 专用
            self._msvcrt = msvcrt
            self._thread = threading.Thread(
                target=self._poll_loop, daemon=True, name="interrupt-listener")
            self._thread.start()
        except ImportError:
            pass

    def _poll_loop(self) -> None:
        """后台轮询:字符进 buffer(无回显),完整行累积。"""
        while not self._stop:
            if not self._paused:
                try:
                    while self._msvcrt.kbhit():
                        ch = self._msvcrt.getwch()
                        if ch in (chr(13), chr(10)):  # CR/LF:行结束标记
                            with self._lock:
                                self._buffer += chr(10)
                            continue
                        if ch == chr(3):  # Ctrl+C
                            self._stop = True
                            return
                        if ch in (chr(8), chr(127)):  # 退格/删除(不回显)
                            with self._lock:
                                self._buffer = self._buffer[:-1]
                        else:  # 字符进 buffer,不回显
                            with self._lock:
                                self._buffer += ch
                except Exception:
                    pass
            time.sleep(0.02)

    def pause(self) -> None:
        """暂停轮询(确认 input() 期间调用,避免抢占 stdin)。"""
        self._paused = True

    def resume(self) -> None:
        """恢复轮询。"""
        self._paused = False

    def take_line(self) -> str | None:
        """消费累积的完整行(检查点调用);无完整行返回 None。"""
        with self._lock:
            if chr(10) in self._buffer:
                line, self._buffer = self._buffer.split(chr(10), 1)
                return line.strip() or None
        return None


class DummyAgent:
    """
    Dummy Agent — 最小可用的 Tool-Calling Agent。

    核心方法：
    - chat(user_input): 处理用户输入，返回最终回答
    - get_history(): 获取当前对话历史
    - reset(): 清空对话历史
    """

    # 最大连续工具调用轮次
    # 防止 agent 陷入无限循环（比如一直调工具但不回答用户）
    MAX_TOOL_TURNS = 40

    def __init__(
        self,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        system_prompt: Optional[str] = None,
    ):
        """
        初始化 Agent。

        参数：
        - llm_client: 配置好的 LLM 客户端实例
        - tool_registry: 注册好工具的注册表实例
        - system_prompt: 可选的 system prompt 覆盖

        === 为什么需要外部传入这些，而不是内部创建？===
        依赖注入（Dependency Injection）模式。
        Agent 不负责创建 LLM 客户端和工具 —— 它只负责"使用"它们。
        好处：
        1. 可测试：可以 mock LLM 客户端来测试 agent 逻辑
        2. 灵活：换模型、换工具都不需要改 agent 代码
        3. 单一职责：Agent 只关心循环逻辑
        """
        self.llm = llm_client
        self.tools = tool_registry
        self.session_store = SessionStore()
        self.current_session_id: Optional[str] = None
        # 跨 Session 记忆抽取器(Phase 2.4)
        self.memory_extractor = MemoryExtractor(self.llm, self.session_store)

        # -------------------------------------------------------
        # 上下文压缩器(Phase 2.2)
        # 临时关闭 L2(enable_l2=False):L2 增量摘要待 Step 5 实现,
        # 开启会触发 NotImplementedError 守卫。Step 5 完成后移除覆盖。
        # -------------------------------------------------------
        self.compressor = ContextCompressor(
            self.llm, CompressionConfig(enable_l2=False)
        )

        # -------------------------------------------------------
        # 构建 system prompt
        # 如果外部传入了，就用外部的；否则自动生成。
        # 自动生成的好处：添加新工具后，system prompt 自动更新。
        # -------------------------------------------------------
        if system_prompt:
            self.system_prompt = system_prompt
        else:
            self.system_prompt = build_system_prompt(
                self.tools.list_tools()
            )

        # -------------------------------------------------------
        # 对话历史
        # 存储结构：list[dict]
        # 每个元素是 OpenAI 格式的消息：
        # {"role": "system"|"user"|"assistant"|"tool", "content": "...", ...}
        # 初始只有 system message。
        # -------------------------------------------------------
        self.history: list[dict] = [
            {"role": "system", "content": self.system_prompt}
        ]

        # Tool 定义（缓存起来，每次调用都传给 LLM）
        self.tool_definitions = self.tools.get_tool_definitions()

        # 随时打断监听器(工具运行中/LLM 等待时用户可输入 /p 触发)
        self._interrupt_listener = _InterruptListener()

        # 注入确认函数提供者:handler 的 _confirm 参数从这里来
        # (输入收集 + /p 拦截在 core,确认语义判断留在 handler)
        self.tools.set_confirm_provider(self._make_confirm())

        # 不在 __init__ 时创建新 session；
        # 只有真正进入 chat() 或 reset() 时才开始持久化。
        # 这样 /resume 可以准确加载此前的会话，而不是把当前空实例
        # 误判成最新会话。

    def chat(self, user_input: str) -> str:
        """
        处理用户的一条消息，返回 Agent 的最终回答。

        参数：
        - user_input: 用户输入的文本消息

        返回值：
        Agent 最终的回答文本。

        === 执行流程 ===
        1. 将用户输入追加到历史
        2. 进入工具调用循环：
           a. 调用 LLM（带工具定义）
           b. 如果返回 tool_calls → 执行 → 回注结果 → 继续循环
           c. 如果返回文本 → 追加到历史 → 返回文本
        3. 如果循环轮次超限，返回超限提示

        === 为什么需要 MAX_TOOL_TURNS？===
        假设用户说"给我讲个故事"，
        理论上 LLM 可以直接回答，不需要调任何工具。
        但如果 LLM 在训练数据中学到"每件事都先 ls 一下"，
        就可能在回答前先调 terminal("ls")。

        如果 LLM 陷入"ls → 看到文件 → 再 ls"的死循环，
        或者在一次推理中产生多个 tool_call，会耗尽 token 配额。

        MAX_TOOL_TURNS 是安全网 —— 限制单次 chat 调用中
        工具调用的轮次上限。
        Hermes 的默认值是 90 轮。
        Phase 0 保守设 10 轮。
        """
        # 先确保当前会话已存在（首次 chat 时才创建）
        self._ensure_session()

        # Phase 2.4:按当前输入检索记忆并注入 system prompt
        # (注入在追加 user 消息之前,LLM 首轮就能看到记忆)
        self._inject_memories(user_input)

        # -------------------------------------------------------
        # Step 1: 追加用户消息
        # 角色是 "user"，这是 OpenAI 格式的要求。
        # 注意 content 可以是字符串，也可以是数组（包含图片等）。
        # Phase 0 只处理纯文本。
        # -------------------------------------------------------
        self.history.append({"role": "user", "content": user_input})
        self._persist_history()

        # -------------------------------------------------------
        # Step 2: 工具调用循环
        # 每次循环：
        #   调用 LLM → 检查是否有 tool_calls →
        #   有则执行并回注 → 继续；无则返回文本。
        # -------------------------------------------------------
        for turn in range(self.MAX_TOOL_TURNS):
            # Phase 2.2:压缩触发点(每次调 LLM 之前检查,
            # 压缩只发生在"即将调 LLM"的静止点,绝不在循环中途)
            self._maybe_compress()

            # 2a. 调用 LLM
            # 传入当前完整历史 + 工具定义
            # LLM 返回的 message 可能包含 content 或 tool_calls
            # strip_meta:压缩摘要消息携带的 _meta 是内部元数据,
            # 发 API 前必须剥离(DeepSeek 对未知顶层字段严格,见 D6)
            response_message = self.llm.chat(
                messages=strip_meta(self.history),
                tools=self.tool_definitions,
                temperature=0.3,  # 工具调用时低温度更稳定
            )

            # 显示思考内容(deepseek 推理模型的 reasoning_content,
            # 非推理模型为 None 时静默跳过)
            reasoning = getattr(self.llm, "last_reasoning", None)
            if reasoning:
                print(f"\n  💭 思考: {reasoning}")

            # -------------------------------------------------------
            # 2b. 检查是否有 tool_calls
            # tool_calls 是一个列表，每个元素是一个工具调用请求
            # {
            #   "id": "call_xxx",
            #   "function": {"name": "terminal", "arguments": "{\"command\": \"ls\"}"},
            #   "type": "function"
            # }
            # -------------------------------------------------------
            if response_message.tool_calls:
                # 先把这个包含 tool_calls 的 assistant 消息追加到历史
                # 这是 OpenAI API 的要求：
                # tool_calls 必须出现在 assistant message 里
                # 注意剥离 reasoning_content:发 API 时未知字段会被拒
                # 或浪费 token(思维链不需要回传)
                msg_dict = response_message.to_dict()
                msg_dict.pop("reasoning_content", None)
                self.history.append(msg_dict)

                # -------------------------------------------------------
                # 2c. 遍历所有 tool_calls 并执行
                # 一个 LLM 响应可能包含多个 tool_calls（虽然 Phase 0 的模型通常一次只调一个）
                # 每个 tool_call 独立执行
                # -------------------------------------------------------
                interrupt_triggered = False
                for tool_call in response_message.tool_calls:
                    # 从 LLM 返回中提取信息
                    tool_name = tool_call.function.name
                    tool_call_id = tool_call.id

                    # arguments 是 JSON 字符串，需要解析
                    # LLM 生成的 JSON 有时不合法（content 中未转义的引号、字面换行符）。
                    # 采用多层容错策略：
                    #   1. 标准 json.loads
                    #   2. json5（允许单引号、尾逗号等）
                    #   3. 正则提取已知键（最坏情况兜底）
                    tool_args = self._parse_tool_arguments(tool_call.function.arguments)

                    # 打印工具调用日志
                    print(f"\n  🛠  Agent 调用了 [{tool_name}] 参数={tool_args}")

                    # ---------------------------------------------------
                    # 分发执行工具（dispatch 内部已做异常兜底和结果规范化）
                    # 确认类工具:dispatch 注入 _confirm(输入收集 + /p 拦截)
                    # InterruptSignal 穿透 dispatch,在这里捕获处理打断
                    # ---------------------------------------------------
                    try:
                        result = self.tools.dispatch(tool_name, tool_args)
                    except InterruptSignal:
                        # 用户 /p 打断(发生在 handler 确认输入时)
                        interrupt_triggered = True
                        self.history.append({
                            "role": "tool", "tool_call_id": tool_call_id,
                            "content": "[用户打断,工具未执行]"})
                        break

                    # 打印执行结果摘要
                    result_preview = result[:300] + "..." if len(result) > 300 else result
                    print(f"  📝  {tool_name} 返回: {result_preview}")

                    # ---------------------------------------------------
                    # 检查点 A:工具执行后,检查监听线程累积的 /p 触发
                    # (工具运行中用户敲 /p,执行完立即消费)
                    # ---------------------------------------------------
                    if not interrupt_triggered:
                        interrupt_triggered = self._drain_interrupt()
                    if interrupt_triggered:
                        self.history.append({
                            "role": "tool", "tool_call_id": tool_call_id,
                            "content": "[用户打断,工具未执行]"})
                        break

                    # -------------------------------------------------------
                    # 2d. 将工具结果以 tool role 回注给 LLM
                    # 这是关键步骤！必须设置正确的 tool_call_id！
                    # tool_call_id 必须和 assistant message 中的 tool_calls[].id 一致。
                    # 否则 LLM 无法把结果和调用关联起来。
                    # -------------------------------------------------------
                    self.history.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": result,
                    })
                    self._persist_history()

                # -------------------------------------------------------
                # 检查点 B:整轮 tool_calls 执行完后(continue 前)
                # -------------------------------------------------------
                if not interrupt_triggered:
                    interrupt_triggered = self._drain_interrupt()

                if interrupt_triggered:
                    # 统一收集提示词(所有打断路径:确认处 /p 与检查点 /p 汇合)
                    prompt = input(
                        "  🧭 检测到 /p 打断,请输入提示词(Enter 取消): "
                    ).strip()
                    if prompt:
                        # 剩余未执行的 tool_calls 补取消消息(消息结构完整)
                        executed_ids = {
                            m.get("tool_call_id")
                            for m in self.history if m.get("role") == "tool"
                        }
                        for tc in response_message.tool_calls:
                            if tc.id not in executed_ids:
                                self.history.append({
                                    "role": "tool", "tool_call_id": tc.id,
                                    "content": "[用户打断,工具未执行]"})
                        # 提示词作为用户消息注入,LLM 下一轮看到插话重新规划
                        print(f"\n  🧭 用户打断: {prompt}")
                        self.history.append({"role": "user", "content": prompt})
                        self._persist_history()
                        continue
                    # 提示词为空:取消打断——取消消息已保留(工具未执行),
                    # 不注入用户消息,继续正常循环(LLM 会自行处理)

                # 工具执行完后，回到循环顶部，再次调 LLM
                # 这次 LLM 能看到工具的执行结果，可以决定下一步
                continue

            # -------------------------------------------------------
            # 2e. LLM 返回的是文本（没有 tool_calls）
            # 这意味着 LLM 认为不需要再调工具了
            # 可以认为这是对用户的最终回答
            # -------------------------------------------------------
            final_text = response_message.content or ""

            # 把最终回答追加到历史（记住 LLM 说了什么）
            self.history.append({"role": "assistant", "content": final_text})
            self._persist_history()

            # Phase 2.4:对话结束抽取记忆(旁路调用,不影响返回)
            self._extract_memories()

            self._save_conversation_log()
            return final_text

        # -------------------------------------------------------
        # 如果循环正常结束但没返回（即轮次用尽仍没得到文本回答）
        # 这通常意味着 agent 陷入了无限工具循环
        # -------------------------------------------------------
        fallback = f"[已达最大工具调用轮次 {self.MAX_TOOL_TURNS}，停止循环]"
        self.history.append({"role": "assistant", "content": fallback})
        self._persist_history()

        self._save_conversation_log()
        return fallback

    def _ensure_session(self) -> None:
        """如果当前 Agent 还没有绑定 session，就创建一个新的会话。"""
        if self.current_session_id is None:
            self.current_session_id = self.session_store.create_session()

    def _persist_history(self) -> None:
        """把当前内存历史同步持久化到 SQLite。"""
        self._ensure_session()
        self.session_store.save_history(self.current_session_id, self.history)

    def _maybe_compress(self) -> None:
        """压缩触发点:工具循环每次调 LLM 之前调用(容错设计 6.x)。

        流程:should_compress(最近一次 usage.prompt_tokens)→ compress →
        成功则替换 history + 落库 + 折叠原文归档(决策 C)→ 事件日志(成败都记)。

        容错原则:压缩失败不能比不压缩更糟——
        - compress() 返回 success=False → 跳过,继续对话
        - compress() 抛异常(如 Step 5 守卫)→ 按失败处理,跳过
        - 落库/归档失败 → 内存已更新,记录警告,下次 persist 重写
        """
        last_tokens = (
            self.llm.last_usage.prompt_tokens if self.llm.last_usage else None
        )
        if not self.compressor.should_compress(last_tokens):
            return

        try:
            result = self.compressor.compress(self.history)
        except Exception as e:
            # 防御:压缩器异常(开发期如 L2 未实现守卫)按失败处理,不拖垮主流程
            self.compressor.register_failure()
            self._log_compression_event(
                None, last_tokens, error_type="compress_exception", error_msg=str(e)
            )
            print(f"⚠️ 压缩异常({type(e).__name__}): 已跳过,对话继续")
            return

        persist_error = None
        if result.success:
            self.history = result.history
            self.compressor.register_success()
            try:
                self._persist_history()
                # 决策 C:persist 重写已删掉折叠前的 tool 原文,这里补回归档表
                if result.folded_originals:
                    self.session_store.archive_tool_results(
                        self.current_session_id, result.folded_originals
                    )
            except Exception as e:
                persist_error = str(e)
                print(f"⚠️ 压缩后落库失败: {e}(内存已更新,下次 persist 重写)")
        else:
            self.compressor.register_failure()
            print(f"⚠️ 压缩失败({result.error_type}): 已跳过,对话继续")

        # 事件日志:成败都记;落库/归档失败也写入 error_type=persist_failed,
        # 使"落库失败率"可统计(plan 6.5,print 不可追溯,error_type 可统计)
        self._log_compression_event(
            result,
            last_tokens,
            error_type="persist_failed" if persist_error else None,
            error_msg=persist_error,
        )

    def _log_compression_event(
        self,
        result: Optional[CompressionResult],
        trigger_tokens: Optional[int],
        error_type: Optional[str] = None,
        error_msg: Optional[str] = None,
    ) -> None:
        """压缩事件写 logs/compression.jsonl(成败都记,容错设计 6.5)。

        用途:复盘压缩行为、统计失败率、调阈值。写日志失败不影响主流程。
        """
        try:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
            os.makedirs(log_dir, exist_ok=True)
            event = {
                "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "session": self.current_session_id,
                "trigger_tokens": trigger_tokens,
                "strategy": result.strategy_used if result else "none",
                "folded": result.folded_count if result else 0,
                "covers": result.summary_covers if result else 0,
                "chars_before": result.chars_before if result else 0,
                "chars_after": result.chars_after if result else 0,
                "success": bool(result and result.success),
                "error_type": error_type or (result.error_type if result else None),
                "error_msg": error_msg or (result.error_msg if result else None),
                "duration_ms": result.duration_ms if result else 0,
            }
            with open(
                os.path.join(log_dir, "compression.jsonl"), "a", encoding="utf-8"
            ) as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"⚠️ 压缩事件日志写入失败: {e}")

    # ---------------------------------------------------------------
    # Phase 2.4: 跨 Session 记忆(注入 + 抽取)
    # ---------------------------------------------------------------
    def _inject_memories(self, user_input: str) -> None:
        """Hermes 方式(分支 hermes-memory-v3):全量常驻注入 + 历史 FTS 兜底。

        主通道:记忆条目全量注入(蒸馏层,写入时合并)。
        兜底通道:问句提炼词搜完整对话历史(messages 源,无损层)——抽取
        波动丢实体时,由历史原始片段救回(2026-08-12 实测三轮 26/26/24
        波动即抽取随机性,历史是唯一永不丢的信息源)。
        user_input 保留签名兼容,用于提炼词。
        """
        mems = self.session_store.list_memories()
        if not mems:
            return
        # 排序:confidence 降序,同置信度新条目优先(重要事实先注入)
        mems.sort(key=lambda m: (-m["confidence"], -m["id"]))
        block_lines = ["", "已知事实(记忆):"]
        total = 0
        ids = []
        for m in mems:
            line = f"- [{m['category']}] {m['fact']}"
            if total + len(line) > 400:  # 记忆段容量上限
                break
            block_lines.append(line)
            total += len(line)
            ids.append(m["id"])
        if not ids:
            return

        # 兜底通道:问句提炼词 → FTS 搜完整历史 → 附加注入(≤200 字符)
        hist_lines = self._retrieve_history_evidence(user_input)

        content = self.history[0]["content"]
        for marker in ("已知事实(记忆):", "相关历史记录:"):
            idx = content.find(marker)
            if idx >= 0:
                content = content[:idx].rstrip()
        new_content = content + "\n" + "\n".join(block_lines)
        if hist_lines:
            new_content += "\n\n相关历史记录:\n" + "\n".join(hist_lines)
        self.history[0] = {"role": "system", "content": new_content}
        self.session_store.increment_memory_hits(ids)
        # 可见性:注入条数 + 历史兜底条数
        print(f"\n  🧠 注入记忆 ({len(ids)}/{len(mems)} 条, ~{total} chars"
              f"{f' + 历史 {len(hist_lines)} 条' if hist_lines else ''}):")
        for line in block_lines[2:]:
            print(f"    {line}")

    def _retrieve_history_evidence(self, user_input: str) -> list[str]:
        """问句提炼词搜完整对话历史(无损兜底层)。

        提炼词失败回退原问句;FTS 失败静默。返回去重片段(≤200 字符)。
        """
        try:
            terms = self.memory_extractor.expand_query(user_input)
        except Exception:
            terms = [user_input]
        hist_lines: list[str] = []
        seen: set[tuple] = set()
        try:
            for term in terms[:2]:
                hits = self.session_store.search(
                    term, source="message", limit=2
                )
                for h in hits:
                    key = (h["session_id"], h["seq"])
                    if key in seen:
                        continue
                    seen.add(key)
                    snippet = (h["snippet"] or "").strip()
                    if not snippet:
                        continue
                    if len("".join(hist_lines)) + len(snippet) > 200:
                        break
                    hist_lines.append(f"- {snippet}")
        except Exception:
            return []
        return hist_lines

    def _extract_memories(self) -> None:
        """对话结束抽取记忆(决策点 1A)。

        旁路调用:抽取结果不污染对话历史;失败由 extractor 内部降级
        (记 memory.jsonl 事件,返回 0)。
        """
        try:
            facts, conflicts = self.memory_extractor.extract_from_history(
                self.history, self.current_session_id
            )
        except Exception:
            return
        if facts:
            print(f"  🧠 已抽取 {facts} 条事实({conflicts} 条覆盖旧事实)")

    def resume_last_session(self) -> bool:
        """恢复数据库中最新的会话历史到当前 Agent。"""
        latest_session_id = self.session_store.get_latest_session_id()
        if not latest_session_id:
            return False
        return self.resume_session(latest_session_id)

    def resume_session(self, session_id: str) -> bool:
        """恢复指定 session_id 的历史到当前 Agent。"""
        history = self.session_store.load_history(session_id)
        if not history:
            return False
        self.current_session_id = session_id
        self.history = history
        return True

    def _save_conversation_log(self):
        """将当前对话历史保存到 logs/ 目录下的 JSON 文件。

        文件名格式: logs/conversation_YYYYMMDD_HHMMSS.json
        包含完整的 role/content/tool_calls 历史，用于 debug。
        """
        import json, os
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(log_dir, f"conversation_{timestamp}.json")

        try:
            with open(log_path, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2, default=str)
        except Exception:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(str(self.history))
            print(f"[警告] 对话历史保存为非 JSON 格式: {log_path}")
            pass  # 日志写入失败不影响主流程

    @staticmethod
    def _parse_tool_arguments(raw: str) -> dict:
        """多层容错解析 LLM 返回的工具参数 JSON。

        策略：
        1. 标准 json.loads
        2. json5.loads（容错单引号、尾逗号、未转义字符等）
        3. 正则提取：找到已知 key（path, content, mode）并提取值
        """
        # 策略 1: 标准 JSON
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # 策略 2: json5
        try:
            import json5
            return json5.loads(raw)
        except Exception:
            pass

        # 策略 3: 正则提取（针对 write_file 等已知结构的扁平 JSON）
        # 当 content 含未转义引号时，标准解析必然失败。
        # 这里利用"已知 key 的顺序"做容错提取。
        result = {}
        try:
            # 提取 path
            m = re.search(r'"(?:path|file)"\s*:\s*"([^"]*)"', raw)
            if m:
                result["path"] = m.group(1)

            # 提取 content（最难：值里可能有未转义引号）
            # 做法：找到 "content": " 之后的内容，然后向前扫描直到
            # 遇到 ", "mode" 或 "}" 或字符串结尾
            cm = re.search(r'"(?:content|text)"\s*:\s*"(.*)', raw, re.DOTALL)
            if cm:
                rest = cm.group(1)
                # 尝试找结束位置：", "mode" 或 "}" 或 ", " 后跟已知 key
                end_patterns = [
                    r'",\s*"(?:mode|path|encoding|append)"',  # , "mode": ...
                    r'"\s*\}\s*$',                          # "} 结尾
                    r'"\s*,\s*\}',                          # ",}
                ]
                content_end = len(rest)
                for pat in end_patterns:
                    m2 = re.search(pat, rest)
                    if m2:
                        content_end = m2.start()
                        break
                result["content"] = rest[:content_end]

            # 提取 mode
            m = re.search(r'"mode"\s*:\s*"([^"]*)"', raw)
            if m:
                result["mode"] = m.group(1)

        except Exception:
            pass

        if result:
            # Unescape JSON escape sequences (strategy 3 regex does not decode them)
            ESCAPE_MAP = {
                "\\n": "\n",    # newline
                "\\r": "\r",    # carriage return
                "\\t": "\t",    # tab
                '\\"': '"',    # double quote
                "\\\\": "\\",  # backslash (must be last)
            }
            for key in result:
                if isinstance(result[key], str):
                    v = result[key]
                    for escaped, actual in ESCAPE_MAP.items():
                        v = v.replace(escaped, actual)
                    result[key] = v
            return result


        # 全部失败
        return {"_error": f"参数 JSON 解析失败: {raw[:100]}"}

    # ---------------------------------------------------------------
    # 统一确认与随时打断(core 只做输入收集与 /p 拦截,语义在 handler)
    # ---------------------------------------------------------------
    def _make_confirm(self):
        """创建注入给 handler 的确认函数(dispatch 时注入 _confirm 参数)。

        职责(两条):
        1. 输入收集:所有确认输入统一经此函数(单点 /p 检查)
        2. /p 拦截:命中 '/p' 前缀抛 InterruptSignal(纯触发,后缀忽略),
           普通输入原样返回,由 handler 处理(y/n/d 等工具语义)

        设计决策(2026-08-14 定稿):确认语义归 handler(每种工具展示
        内容不同),/p 拦截归 core(全局一致)——handler 不裸调 input(),
        打断不可能被 handler 内部逻辑误用。打断触发后提示词由 chat
        循环打断处理点统一收集(阻塞式 input)。
        """
        def confirm(prompt: str) -> str:
            self._interrupt_listener.pause()  # 暂停轮询,避免与 input 竞争
            try:
                print(prompt)
                raw = input(
                    "     或输入 /p 打断(随后输入提示词): "
                ).strip()
            finally:
                self._interrupt_listener.resume()
            if raw.startswith("/p"):
                raise InterruptSignal()  # 纯触发,提示词在打断处理点统一收集
            return raw
        return confirm

    def _drain_interrupt(self) -> bool:
        """检查点调用:监听线程累积的行中是否有 /p 触发。

        实时监听只认 '/p' 前缀(后面的内容全部忽略,不解析为提示词);
        触发后提示词由打断处理点统一收集(阻塞式 input)。
        """
        line = self._interrupt_listener.take_line()
        return bool(line and line.startswith("/p"))

    def get_history(self) -> list[dict]:
        """
        获取完整的对话历史（包括 system prompt）。
        用于调试或后续的持久化存储。
        """
        return list(self.history)

    def reset(self):
        """
        重置对话历史，只保留 system prompt。
        相当于重新开始对话，但保留工具注册等配置。
        """
        self.history = [
            {"role": "system", "content": self.system_prompt}
        ]
        self.current_session_id = self.session_store.create_session()
        self._persist_history()

"""tool_guardrails.py — 工具循环护栏（P1a）

============ 这个模块解决什么问题 ============
LLM 在工具循环里会有两类"原地打转"：

  1. 同一份失败反复出现
     同一个工具、同一套参数、**连报错内容都一样**,一次次原样重试。
     实例：日志里 write_file 连续 3 次空参数被 TypeError 打回；
           terminal 里那条卡住的 powershell 反复重跑。
     ⚠️ 判据必须包含"报错也一样":只按工具+参数计数会把
     "改一处、再跑一次测试"(报错每次都不同)误判成打转并拦死 ——
     而验证门又要求"跑通全量测试",两者会互锁。见 after_call 里的注释。

  2. 只读调用毫无进展
     只读工具连续返回**完全相同**的结果，模型还一遍遍要。
     实例：p1.json 里搜索结果不好，换个说法接着搜，
           连着 4~5 轮无效搜索，上下文越堆越大还不止损。

护栏在**循环内实时**数这两类信号，达阈值就告警/拦截。

============ 与 dummy 已有的自检层有什么区别 ============
dummy 已有 self_improve.detect_tool_issue（错误率 > 30% 告警）。两者不是一回事：

  - 那层：按工具名的**会话累计**成功率，每轮 chat 开头检查一次，
          动作是**打印一行提示人去改工具代码**（跨会话、面向开发者）；
  - 本层：按**具体调用签名**的循环内计数，动作是**约束模型本身**
          （单轮内、面向 LLM，止损）。

一句话：那层是"这工具最近老坏，你去改改"，本层是"你第 5 次踩同一个坑了，
这条我不执行"。两者都要有。

============ 设计（对齐 Hermes agent/tool_guardrails.py） ============
- **本模块纯粹无副作用**：只记账、只返回决策，不执行工具、不打印。
  运行时（core 的工具循环）决定把决策变成告警文本、合成结果还是拦截。
  这样护栏本身可单测，不需要 LLM、不需要文件系统。
- 两个旋钮分离：
    warnings_enabled  — 告警**永不阻止执行**，只是把提示追加进工具结果里
                        让模型看到（所以"只告警"并非无用：它是发给模型的）
    hard_stop_enabled — 真正的拦截（block）才需要打开
- 成功即清零：某签名成功一次，它的失败计数与无进展记录都清掉。
  这样"先失败、修好后成功、之后再失败"不会被累计误判（见 after_call）。

============ 未实现（用户 2026-09-14 判定"有待商榷"，暂不做） ============
- same_tool_failure（同一工具失败 N 次就 halt，**不看参数**）：
  "重试"本身有合法用途（等外部状态变化），只按工具名计数容易误伤。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from lessons import is_tool_error


# ---------------------------------------------------------------
# 工具分类
# ---------------------------------------------------------------
# 只读工具：同样参数必然同样结果，重复调用 = 无进展（可安全拦截）
IDEMPOTENT_TOOLS = frozenset({
    "read_file",
    "web_search",
    "web_extract",
})

# 有副作用工具：不参与"无进展"判定（重试可能有合法意图）
MUTATING_TOOLS = frozenset({
    "terminal",
    "write_file",
})


def _env_flag(name: str, default: bool) -> bool:
    """读环境变量开关；未设置或无法识别时取默认值。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int) -> int:
    """读环境变量整数；非法或 < 1 时取默认值。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


@dataclass(frozen=True)
class GuardrailConfig:
    """护栏阈值。

    默认策略（dummy 有意偏离 Hermes 之处，需要时改回即可）：
    - Hermes 默认 hard_stop_enabled=False（多端产品，怕拦截破坏自动化流程）
    - dummy 默认 **True**：单用户交互式，且目标就是止损省轮次。
      想退化成"只告警不拦截"：设 DUMMY_GUARDRAIL_HARD_STOP=0
    """

    warnings_enabled: bool = True
    hard_stop_enabled: bool = True

    # 完全相同失败(工具 + 参数 + **报错内容** 三者一致)
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5

    # 只读工具无进展（结果完全一致）
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5

    idempotent_tools: frozenset[str] = field(default_factory=lambda: IDEMPOTENT_TOOLS)
    mutating_tools: frozenset[str] = field(default_factory=lambda: MUTATING_TOOLS)

    @classmethod
    def from_env(cls) -> "GuardrailConfig":
        """从环境变量构造（便于临时调参与关闭，不改代码）。"""
        defaults = cls()
        return cls(
            warnings_enabled=_env_flag(
                "DUMMY_GUARDRAIL_WARN", defaults.warnings_enabled),
            hard_stop_enabled=_env_flag(
                "DUMMY_GUARDRAIL_HARD_STOP", defaults.hard_stop_enabled),
            exact_failure_warn_after=_env_int(
                "DUMMY_GUARDRAIL_EXACT_WARN", defaults.exact_failure_warn_after),
            exact_failure_block_after=_env_int(
                "DUMMY_GUARDRAIL_EXACT_BLOCK", defaults.exact_failure_block_after),
            no_progress_warn_after=_env_int(
                "DUMMY_GUARDRAIL_PROGRESS_WARN", defaults.no_progress_warn_after),
            no_progress_block_after=_env_int(
                "DUMMY_GUARDRAIL_PROGRESS_BLOCK", defaults.no_progress_block_after),
        )


@dataclass(frozen=True)
class ToolCallSignature:
    """一次调用的稳定标识：工具名 + 参数规范化后的哈希（不可逆）。"""

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Mapping[str, Any] | None) -> "ToolCallSignature":
        return cls(tool_name=tool_name, args_hash=_sha256(canonical_tool_args(args)))

    def __str__(self) -> str:
        return f"{self.tool_name}#{self.args_hash[:8]}"


@dataclass(frozen=True)
class GuardrailDecision:
    """护栏决策。action: allow | warn | pause

    ============ 为什么是 pause 而不是 block ============
    原设计用 block:第 N 次起**不再执行**该调用,直到轮次用尽。
    这有两个问题:
      1. 它是**判决**——"你在打转,不许再跑",模型无法反驳,只能绕路;
         而"这次重复是否合理"其实是**语义判断**,该由模型自己做。
      2. 它和别的机制会互锁(历史实测:护栏禁止跑测试,验证门又要求跑通测试)。

    现在改为 pause:**只拦下这一次**,并把事实(重复了几次、结果长什么样)
    如实报告给模型,然后**循环照常继续**。模型下一步自己决定:
      - 改变参数再试    → 正常继续(护栏不阻止)
      - 说明理由后重跑  → 也行(护栏只是再报告一次)
      - 输出文本收尾    → 也行
    **不需要任何"继续"信号** —— 模型的行为本身就是答案。
    """

    action: str = "allow"
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: ToolCallSignature | None = None

    @property
    def allows_execution(self) -> bool:
        """allow / warn 允许执行；pause 拦下这一次（但不终止循环）。"""
        return self.action in {"allow", "warn"}


def canonical_tool_args(args: Mapping[str, Any] | None) -> str:
    """参数规范化：排序键 + 紧凑 JSON，保证"同参数"判定稳定。"""
    if not isinstance(args, Mapping):
        return "{}"
    return json.dumps(
        args, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )


class ToolCallGuardrailController:
    """按**单轮**统计的护栏控制器（每轮 chat 开始时 reset_for_turn）。"""

    def __init__(self, config: GuardrailConfig | None = None):
        self.config = config or GuardrailConfig()
        self.reset_for_turn()

    def reset_for_turn(self) -> None:
        self._exact_failures: dict[ToolCallSignature, tuple[str, int]] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}

    # -----------------------------------------------------------
    # 调用前：判断这条要不要拦
    # -----------------------------------------------------------
    def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> GuardrailDecision:
        signature = ToolCallSignature.from_call(tool_name, args)
        if not self.config.hard_stop_enabled:
            return GuardrailDecision(tool_name=tool_name, signature=signature)

        count = self._exact_failures.get(signature, (None, 0))[1]
        if count >= self.config.exact_failure_block_after:
            decision = GuardrailDecision(
                action="pause",
                code="repeated_same_failure",
                message="",   # 由 Agent 用 build_report() 渲染
                tool_name=tool_name, count=count, signature=signature,
            )
            return decision

        if self._is_idempotent(tool_name):
            record = self._no_progress.get(signature)
            if record is not None:
                _result_hash, repeat = record
                if repeat >= self.config.no_progress_block_after:
                    decision = GuardrailDecision(
                        action="pause",
                        code="idempotent_no_progress",
                        message="",   # 由 Agent 用 build_report() 渲染
                        tool_name=tool_name, count=repeat, signature=signature,
                    )
                    return decision

        return GuardrailDecision(tool_name=tool_name, signature=signature)

    # -----------------------------------------------------------
    # 报告文案：陈述事实，不下判决
    # -----------------------------------------------------------
    def build_report(self, decision: GuardrailDecision,
                     extra_facts: list[str] | None = None) -> str:
        """把护栏决策渲染成发给模型的**事实报告**(公开入口,供 Agent 调用)。

        extra_facts 由 Agent 补充"护栏自己不知道"的事实
        (如"这期间你改动过 N 个文件")。
        """
        fact = (f"{decision.tool_name} 连续 {decision.count} 次返回完全相同的结果"
                if decision.code == "idempotent_no_progress"
                else f"{decision.tool_name} 连续 {decision.count} 次返回完全相同的失败")
        lines = [f"[护栏提示] {fact}。", "", f"· 工具: {decision.tool_name}",
                 f"· 连续相同次数: {decision.count}"]
        for f in (extra_facts or []):
            lines.append(f"· {f}")
        lines += [
            "",
            "请根据以上事实自行判断:如果你确实需要再看一次,可以换个参数或"
            "说明理由后继续;如果已有信息够用,直接给出结论即可。",
        ]
        return "\n".join(lines)

    # -----------------------------------------------------------
    # 调用后：记账 + 判断要不要报告
    # -----------------------------------------------------------
    def after_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result: str | None,
        *,
        failed: bool | None = None,
    ) -> GuardrailDecision:
        signature = ToolCallSignature.from_call(tool_name, args)
        if failed is None:
            failed = is_tool_error(result or "")

        if failed:
            # 只把"**同一份失败**反复出现"算作打转:同工具+同参数还不够——
            # "改一处、再跑一次测试"的报错每次都不同,那是正常迭代;只按参数
            # 计数会把它判成循环并拦死(实测会与验证门互锁:门要求跑通测试,
            # 护栏却禁止再执行该命令)。形状与下面只读工具的 no_progress 一致:
            # 结果指纹 + 连续次数。
            result_hash = _result_hash(result)
            previous = self._exact_failures.get(signature)
            count = previous[1] + 1 if previous and previous[0] == result_hash else 1
            self._exact_failures[signature] = (result_hash, count)
            # 一旦失败，此前的"无进展"记录作废（失败≠无进展）
            self._no_progress.pop(signature, None)

            if self.config.warnings_enabled and count >= self.config.exact_failure_warn_after:
                return GuardrailDecision(
                    action="warn",
                    code="repeated_same_failure_warn",
                    message="",   # 由 Agent 用 build_report() 渲染
                    tool_name=tool_name, count=count, signature=signature,
                )
            return GuardrailDecision(tool_name=tool_name, count=count, signature=signature)

        # ---- 成功：清零该签名的失败记录 ----
        # 这一步很关键：“先失败 → 修好 → 成功 → 之后再失败”不会被累计误判，
        # 也避免"文件创建后重新读取成功"这类合法重试被拦。
        self._exact_failures.pop(signature, None)

        if not self._is_idempotent(tool_name):
            self._no_progress.pop(signature, None)
            return GuardrailDecision(tool_name=tool_name, signature=signature)

        # 只读工具：比较结果指纹，判断是否"毫无进展"
        result_hash = _result_hash(result)
        previous = self._no_progress.get(signature)
        repeat = 1
        if previous is not None and previous[0] == result_hash:
            repeat = previous[1] + 1
        self._no_progress[signature] = (result_hash, repeat)

        if self.config.warnings_enabled and repeat >= self.config.no_progress_warn_after:
            return GuardrailDecision(
                action="warn",
                code="idempotent_no_progress_warning",
                message=(
                    f"{tool_name} 已 {repeat} 次返回相同结果，没有新信息。"
                    "请使用已拿到的结果，或换查询条件。"
                ),
                tool_name=tool_name, count=repeat, signature=signature,
            )
        return GuardrailDecision(tool_name=tool_name, count=repeat, signature=signature)

    def _is_idempotent(self, tool_name: str) -> bool:
        """只读判定：显式免疫有副作用工具，其余必须在只读白名单里。"""
        if tool_name in self.config.mutating_tools:
            return False
        return tool_name in self.config.idempotent_tools


# ---------------------------------------------------------------
# 运行时辅助（把"决策"翻译成能塞回历史的东西）
# ---------------------------------------------------------------
def append_guidance(result: str, decision: GuardrailDecision) -> str:
    """把护栏报告追加到真实工具结果末尾——这是发给 LLM 看的，不是打印给人。

    报告文本由 Agent 渲染（见 ToolCallGuardrailController.build_report），
    因为 Agent 才知道"这期间改过哪些文件"这类护栏看不到的事实。
    """
    if not decision.message:
        return result
    return f"{result}\n\n{decision.message}"


# ---------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------
def _result_hash(result: str | None) -> str:
    """结果指纹。

    先尝试 JSON 解析再规范化：语义相同但键序/空白不同的 JSON
    会被判为"同一结果"（对齐 Hermes 的做法）。
    解析不了就用原串（dummy 的工具多数返回纯文本，走这条路）。
    """
    text = result or ""
    canonical = text
    stripped = text.strip()
    if stripped[:1] in "{[":
        try:
            parsed = json.loads(stripped)
            canonical = json.dumps(
                parsed, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str,
            )
        except Exception:
            canonical = text
    return _sha256(canonical)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

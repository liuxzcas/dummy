"""turn_context.py — 本轮情况说明(替代 verify_stop 的判决角色)

============ 为什么要改 ============
`verify_stop.py` 原本是**判决者**:346 行硬规则(全量/收尾/晚于编辑/
豁免名单/上限 2 次),对"任务完成了吗""什么算证据"下判决,然后把结论
当驳回塞回模型。

三个实测 bug 证明这条路走不通——它们其实是**同一个病的三种症状**:
规则枚举不完现实。
  - `.bat` 不在豁免名单 → 让 pytest 去证明一个启动器能不能跑(荒谬)
  - "证据只能是全量 pytest" → 模型做的相关验证(drill/换行检查)全不被认
  - "上限 2 次"按轮清零 → 用户插一句话,安全阀重置

**根因**:这些判断被硬编码成规则,而现实总有枚举不到的情况。

============ 新方向 ============
把判断**还给 LLM**,agent 只做它擅长的:提供运行过程中产生的信息。

  ❌ 旧:门判决"你没证据,驳回"     → 模型无法反驳,只能去满足规则
  ✅ 新:门陈述"本轮改了什么、跑了什么"→ 模型自己判断够不够

这与项目既有原则一致:**可见性优先**(把事实摆出来,由人/模型判断),
而不是代替判断。

============ 边界(重要) ============
本模块**只陈述事实,不下判决**:
  - 不判"完成了没有"
  - 不判"证据够不够"
  - 不驳回、不计次、不设上限

模型拿到情况说明后可以:
  - 认为不够 → 继续验证
  - 认为够了 → 解释为什么,然后收尾
  - 认为任务本身不该这么验 → 说出来

**例外**:那些"不是语义判断"的事仍由代码硬保证,不归本模块管:
  - 历史协议合法性(tool_calls↔tool 配对)——API 协议,LLM 不能违反
  - 安全确认(要不要执行 rm)——人的判断
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 观测记录的最大条数(防止上下文被撑爆;超出只保留最近的)
MAX_RUNS = 12
# 单条观测摘要的最大字符数
MAX_DIGEST = 160


def _digest(text: str | None) -> str:
    """把一段输出压成一行摘要。"""
    flat = " ".join((text or "").split())
    return flat[:MAX_DIGEST] + ("…" if len(flat) > MAX_DIGEST else "")


@dataclass
class RunRecord:
    """一次工具执行留下的记录(只记事实,不判成败)。"""

    tool: str
    summary: str          # 命令 / 路径等"做了什么"
    outcome: str          # 结果摘要(原始输出的一行)
    report: str | None = None   # 结构化回报(如终端的退出码行、写入成功标记)

    def render(self) -> str:
        line = f"- {self.tool}: {self.summary}"
        if self.outcome:
            line += f"\n    → {self.outcome}"
        if self.report:
            line += f"\n    ({self.report})"
        return line


@dataclass
class TurnContext:
    """本轮运行情况的观察记录器。

    只累积事实,不做任何判定。**没有 max_attempts,没有驳回,没有豁免名单。**
    """

    changed: list[str] = field(default_factory=list)   # 本轮改动的文件
    runs: list[RunRecord] = field(default_factory=list)
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "TurnContext":
        """DUMMY_TURN_CONTEXT=0 可关掉(默认开)。"""
        import os
        enabled = os.environ.get("DUMMY_TURN_CONTEXT", "1").strip().lower() \
            not in {"0", "false", "no", "off", ""}
        return cls(enabled=enabled)

    def reset_for_turn(self) -> None:
        """每轮开头清零(本轮的观察只属于本轮)。"""
        self.changed = []
        self.runs = []

    # ---------------------- 记录事实 ----------------------
    def note_tool_result(self, tool_name: str,
                         args: dict[str, Any] | None,
                         result: str | None) -> None:
        """记一次工具执行:做了什么 + 结果长什么样。

        **刻意不做"成功/失败"判定**——把原始结果摘要摆出来,
        由模型自己读。退出码、错误文本都在 outcome 里,模型看得见。
        """
        args = args or {}
        if tool_name == "write_file":
            path = args.get("path")
            if path:
                self.changed.append(str(path))
            what = f"写入 {path}" if path else "写入"
            self._add(tool_name, what, _digest(result))
            return

        if tool_name == "terminal":
            self._add(tool_name, str(args.get("command", ""))[:120],
                      _digest(result))
            return

        if tool_name == "read_file":
            self._add(tool_name, str(args.get("path", ""))[:120],
                      _digest(result))
            return

        self._add(tool_name, str(args)[:80], _digest(result))

    def _add(self, tool: str, summary: str, outcome: str) -> None:
        if not self.enabled:
            return
        self.runs.append(RunRecord(tool=tool, summary=summary, outcome=outcome))
        if len(self.runs) > MAX_RUNS:
            del self.runs[0]

    # ---------------------- 产出情况说明 ----------------------
    def build_stop_context(self) -> str | None:
        """模型想收尾时,给一份**本轮情况说明**(陈述,非判决)。

        返回 None 的情况:本轮什么都没做(不值得打扰)。
        只要有过改动或执行,就把情况如实摆出来——**说过什么由模型判断**。
        """
        if not self.enabled or (not self.changed and not self.runs):
            return None

        lines: list[str] = ["[本轮运行情况]"]

        if self.changed:
            lines.append("本轮改动/新建的文件:")
            for p in dict.fromkeys(self.changed):
                lines.append(f"- {p}")

        if self.runs:
            lines.append("本轮执行记录(按时间顺序,最近 %d 条):" % MAX_RUNS)
            for r in self.runs:
                lines.append(r.render())

        lines.append(
            "对照上面的记录核对你刚才的说法,不一致就改述;"
            "没做/没验的直说,别写成已完成(需要补验现在还来得及)。"
            "**核对的结论用一句话带过就行,不必逐条复述。**"
        )
        lines.append(
            "然后另起一段,用「## 结论」开头,给用户一个最终答复。"
            "**只写用户需要知道的,不要重复你上面已经说过的内容**:"
            "做完了什么(一句话)、怎么用、哪些没验证需要用户确认。"
            "如果你刚才的回答已经说清楚且没有需要更正的地方,"
            "这一段就写短一点,只保留结论和待确认项即可。"
        )
        return "\n".join(lines)

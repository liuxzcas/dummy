"""verify_stop.py — 收尾前验证门(P1b,档 A)

============ 解决什么问题 ============
模型改完代码,回一段"我改好了"就收工——但它并没有跑过任何东西。
dummy 里这类事反复发生(日志实证):
  - 雷战机那次:改完黑屏,说"修好了",你打开还是黑屏
  - 网页/PPT 那次:文件写出来语法没错但跑不起来,来回返工

本模块在**模型想收尾的那一刻**插一道门:本轮改过代码却拿不出通过证据 -> 驳回,
要求先验证再宣布完成。

============ "严"的三个轴(调研结论,详见 docs/p1b-verification-design.md) ============
"严"不是一个旋钮,而是三个独立轴:
  ① 证据内容   自述"跑过了" -> 规范命令 exit code -> 真测试输出 -> 端到端外部证据
  ② 判决者     同一模型自评 -> 独立 critic 模型 -> **确定性脚本(退出码)**
  ③ 强制       提示词引导(可跳过) -> 钩子(可配置) -> 管理员锁 + 次数上限

业界六家(Claude Code / Codex / Aider / OpenHands / AlphaCodium / Hermes)的"严"
**没有一家是靠"更聪明的评委"**,全是把判决外包给一个退出码。
学术依据:无外部反馈的自我纠错不仅不提升反而更差(Huang 2024, arXiv:2310.01798);
critique 只有锚定外部工具才有效(CRITIC, arXiv:2305.11738)。

所以 dummy 的严加在 ① 证据内容上,而不是把阈值调小。

============ 档 A 规格(用户 2026-09-15 选定) ============
本轮的通过证据 = 一次**完整测试套件**的 pytest 执行且成功:
  - 命令须是"全量"调用:含 pytest,且不指定单个测试文件/单个用例
    (`python -m pytest tests/ -q` 算;`pytest tests/test_x.py` 不算)
  - 必须是**本轮改动之后**产生的(编辑会让先前证据失效)
  - 必须留痕:记录命令原文 + 输出摘要 + 序号
为什么"全套跑绿"就够严:全套通过隐含覆盖了被改动的文件,不需要维护
"模块->测试"的映射表。代价是每次改 .py 都要跑一遍(136 测试约 18 秒)。

配套(业界高度一致,非可选项):
  - **次数上限**(本模块默认 2;业界取值 2/3/3/8,都有兜底)
  - **防重入**:上限计数天然扮演 Claude Code `stop_hook_active` 的角色
  - **文档类豁免**:.md/.txt/.csv 等无运行时行为的文件不驳回

============ 与 Hermes 的差异 ============
1. Hermes 只要求"跑过项目的规范命令",**不校验覆盖范围** -> 跑无关脚本也算数,
   属于"证据仍靠自述"(严格度阶梯 L3)。本模块把它提到 L5 的证据轴:
   必须全量测试通过。
2. Hermes 会探测项目规范命令(pytest/npm/make),dummy 简化成硬编码规则
   (含 pytest 且不指定单文件),够用且零维护。
3. Hermes 把原答案降级保存为兜底;dummy 直接把答案与驳回消息都写进历史
   (模型能看到自己刚才的声明,便于修正),简单且不丢信息。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

from lessons import is_tool_error


# ---------------------------------------------------------------
# 文档类豁免(照 Hermes 的白名单:无运行时行为可验)
# ---------------------------------------------------------------
NON_CODE_EXTENSIONS = frozenset({
    ".md", ".markdown", ".mdx", ".rst", ".txt", ".text",
    ".adoc", ".asciidoc", ".org", ".log", ".csv", ".tsv",
})

NON_CODE_FILENAMES = frozenset({
    "license", "licence", "notice", "authors",
    "contributors", "changelog", "codeowners",
})

# 改动后需要验证的写入模式说明写在 write_file 的返回串里:成功串含 "成功: "
_EDIT_SUCCESS_MARKER = "成功: "


def is_verifiable_path(raw: str | None) -> bool:
    """改动路径是否"有运行时行为可验证"。

    文档/散数据类返回 False(改 README 不该被要求跑测试)。
    """
    if not raw:
        return False
    name = os.path.basename(str(raw)).lower()
    ext = os.path.splitext(name)[1]
    if ext in NON_CODE_EXTENSIONS:
        return False
    if not ext and name in NON_CODE_FILENAMES:
        return False
    return True


def is_write_success(result: str | None) -> bool:
    """write_file 是否真的写成功了(跳过/取消/报错都不算改动)。"""
    text = result or ""
    return _EDIT_SUCCESS_MARKER in text and not is_tool_error(text)


# 会"缩小测试范围"的 pytest 参数:命中任意一个即不算全量套件。
# 依据:全量套件之所以能当证据,是因为"全绿隐含覆盖了被改动的文件";
# 一旦用 -k/--lf 之类把子集挑出来,这个蕴含关系就不成立了。
_NARROWING_FLAGS = frozenset({"-k", "--keyword", "--deselect", "--lf", "--last-failed"})


def is_full_suite_pytest_command(command: str | None) -> bool:
    """命令是否是一次"全量测试套件"调用。

    判据(启发式,不追求做完整的命令行解析器):
      - 含 pytest
      - 不含 node id(``::``),即不指定单个用例
      - 不出现单个 .py 文件,即不指定单个测试文件
      - 不含缩小范围的参数(见 _NARROWING_FLAGS);
        另外 ``-m <标记>`` 也算缩小,但 ``-m pytest`` 是模块调用形式,不算
    """
    text = (command or "").strip()
    if "pytest" not in text:
        return False
    if "::" in text:
        return False

    tokens = text.split()
    for i, token in enumerate(tokens):
        if token in _NARROWING_FLAGS:
            return False
        if token.endswith(".py"):
            return False
        if token == "-m" and (tokens[i + 1] if i + 1 < len(tokens) else "") != "pytest":
            return False
    return True


@dataclass
class VerificationEvidence:
    """一次通过的验证记录(留痕:命令 + 输出摘要 + 序号)。"""

    command: str
    summary: str
    seq: int

    def describe(self) -> str:
        return f"`{self.command}` (输出摘要: {self.summary})"


@dataclass
class VerificationLedger:
    """按**单轮**记账的验证账本(每轮 chat 开头 reset_for_turn)。

    只记账、只给判决,不执行命令、不打印——便于脱离 LLM 单测。
    """

    max_attempts: int = 2
    enabled: bool = True
    _changed: list[str] = field(default_factory=list)
    _evidence: VerificationEvidence | None = None
    _attempts: int = 0
    _seq: int = 0
    _last_edit_seq: int = -1

    @classmethod
    def from_env(cls) -> "VerificationLedger":
        """从环境变量构造:DUMMY_VERIFY_ON_STOP=0 关掉,DUMMY_VERIFY_MAX_ATTEMPTS 改上限。"""
        enabled = os.environ.get("DUMMY_VERIFY_ON_STOP", "1").strip().lower() \
            not in {"0", "false", "no", "off", ""}
        raw = os.environ.get("DUMMY_VERIFY_MAX_ATTEMPTS")
        try:
            attempts = int(raw) if raw is not None else 2
        except ValueError:
            attempts = 2
        return cls(max_attempts=max(1, attempts), enabled=enabled)

    def reset_for_turn(self) -> None:
        self._changed = []
        self._evidence = None
        self._attempts = 0
        self._seq = 0
        self._last_edit_seq = -1

    # ---------------------- 记账 ----------------------
    def note_tool_result(self, tool_name: str, args: dict[str, Any] | None, result: str | None) -> None:
        """工具执行成功后记账:改动 -> 记路径;全量测试通过 -> 记证据。"""
        self._seq += 1
        if tool_name == "write_file":
            if is_write_success(result):
                path = (args or {}).get("path")
                if path:
                    self._changed.append(str(path))
                    self._last_edit_seq = self._seq
            return

        if tool_name == "terminal":
            command = (args or {}).get("command")
            if is_full_suite_pytest_command(command) and not is_tool_error(result or ""):
                self._evidence = VerificationEvidence(
                    command=str(command).strip(),
                    summary=_digest(result),
                    seq=self._seq,
                )

    # ---------------------- 判决 ----------------------
    @property
    def changed_paths(self) -> list[str]:
        return list(dict.fromkeys(self._changed))

    def has_fresh_evidence(self) -> bool:
        """证据必须晚于最后一次编辑——"先跑测试再改代码"不算数。"""
        return self._evidence is not None and self._evidence.seq > self._last_edit_seq

    def build_stop_nudge(self) -> str | None:
        """收尾前调用:该驳回就返回驳回文本并计一次;否则返回 None(放行)。"""
        if not self.enabled or self._attempts >= self.max_attempts:
            return None

        paths = [p for p in self.changed_paths if is_verifiable_path(p)]
        if not paths:
            return None                       # 没改可验证文件(或只改文档) -> 放行
        if self.has_fresh_evidence():
            return None                       # 有新鲜通过证据 -> 放行

        self._attempts += 1
        return _nudge_text(paths, self._evidence, self._attempts, self.max_attempts)


def _digest(result: str | None) -> str:
    """输出摘要:压缩空白后取前 200 字符,附指纹便于核对。"""
    text = " ".join((result or "").split())
    head = text[:200] + ("..." if len(text) > 200 else "")
    return f"{head} [sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}]"


def _nudge_text(paths: list[str], evidence: VerificationEvidence | None,
                attempt: int, max_attempts: int) -> str:
    """驳回文本。要点:说清改了什么、要做什么、以及"无法验证就说卡点"的出口。"""
    shown = paths[:8]
    lines = [f"- `{p}`" for p in shown]
    if len(paths) > len(shown):
        lines.append(f"- ... 另有 {len(paths) - len(shown)} 个")
    state = f"已有证据但已过期(改动晚于它): {evidence.describe()}" if evidence \
        else "本轮还没有任何验证证据"

    return (
        "[系统: 你在本轮修改了代码,但没有可验证的通过证据,暂不接受收工。\n\n"
        f"本轮改动的文件:\n" + "\n".join(lines) + "\n\n"
        f"当前证据状态: {state}\n\n"
        "请用 terminal 运行**完整测试套件**并让它通过(退出码 0),例如:\n"
        "    python -m pytest tests/ -q\n"
        "注意:只跑单个测试文件不算证据(覆盖不到你的改动);"
        "跑与改动无关的脚本也不算。\n\n"
        "如果你确实无法验证,请直接说明具体卡点(缺依赖/缺环境/改动没有运行时行为),"
        "不要声称已完成。\n\n"
        f"(第 {attempt}/{max_attempts} 次提醒;达到上限后不再打断)]"
    )

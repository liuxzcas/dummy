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
  - 命令须**以全量 pytest 调用收尾**:不指定单个测试文件/单个用例,
    不含缩小范围的参数。为什么盯"最后一段 shell 命令":shell 报告的退出码
    就是最后一段的退出码,只有最后一段是 pytest,"退出码=0"才等价于
    "测试通过"。反例 `pytest … | tail` 的退出码是 tail 的(恒为 0),
    会把**失败的测试记成通过**(实测确认过,2026-09-15)。
  - 该段须**直接调用** pytest:认 `pytest` / `.venv/bin/pytest` /
    `python -m pytest`;`echo pytest` 不算。
  - 必须是**本轮改动之后**产生的(编辑会让先前证据失效)。
  - 必须**真的执行过**:terminal 返回串带 "[SHELL:" 标记。
    用户按 n 取消时返回 "[用户取消] 命令未执行",它不含 [SHELL: 也不含任何
    错误特征——不加这条,一条被取消、从未运行的 pytest 会被误记为通过。
  - 必须留痕:记录命令原文 + 输出摘要 + 序号。
为什么"全套跑绿"就够严:全套通过隐含覆盖了被改动的文件,不需要维护
"模块->测试"的映射表。代价是每次改 .py 都要跑一遍(全套一百多项,约十几秒)。

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
import re
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

# shell 分隔符:&& || ; | —— 刻意不含单独的 &,免得把 `2>&1` 这类重定向切开
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\|")

# 前置环境变量赋值(如 PYTHONPATH=.),识别调用时跳过
_ENV_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")

# 能启动 pytest 的解释器
_PY_LAUNCHERS = frozenset({"python", "python3", "python.exe", "python3.exe", "py"})

# terminal 真正执行过时,返回串首行是 "[SHELL: xxx]";取消执行则返回
# "[用户取消] 命令未执行"。用它区分"真跑过"与"根本没跑"。
_TERMINAL_EXECUTED_MARKER = "[SHELL:"


def _is_pytest_invocation(segment: str) -> bool:
    """该 shell 段是否**直接调用** pytest(而不是把 pytest 当普通参数)。

    认这几种形式:`pytest ...`、`.venv/bin/pytest ...`、
    `python -m pytest ...`(含 python3/python.exe/py)。
    这样 `echo pytest` 之类不会被误判成跑过测试。
    """
    tokens = segment.strip().split()
    i = 0
    while i < len(tokens) and _ENV_ASSIGN.fullmatch(tokens[i]):
        i += 1
    rest = tokens[i:]
    if not rest:
        return False
    head = rest[0]
    if head == "pytest" or head.endswith("/pytest") or head.endswith("\\pytest"):
        return True
    return head in _PY_LAUNCHERS and rest[1:3] == ["-m", "pytest"]


def is_full_suite_pytest_command(command: str | None) -> bool:
    """命令是否"以一次全量 pytest 调用收尾"。

    ============ 为什么盯**最后一段** shell 命令 ============
    shell 报告的退出码就是最后一段的退出码。只有最后一段是 pytest,
    "退出码 = 0"才等价于"测试通过"。反例(实测确认过):

        python -m pytest tests/ -q 2>&1 | tail -3

    管道的退出码是 tail 的(恒为 0),于是**测试失败也会显示退出码 0**,
    被记成"验证通过"。而 `pytest ... | tail` 是极常见的写法,不是边角案例。
    同理 `pytest ...; echo done` 也会被 echo 的成功掩盖。

    另:该段还必须**直接调用** pytest(`echo pytest` 不算);且不含
    node id(::)、单个 .py 文件、缩小范围的参数(见 _NARROWING_FLAGS)。
    """
    text = (command or "").strip()
    if "pytest" not in text:
        return False

    segment = _SEGMENT_SPLIT.split(text)[-1].strip()
    if not _is_pytest_invocation(segment):
        return False
    if "::" in segment:
        return False

    tokens = segment.split()
    for i, token in enumerate(tokens):
        if token in _NARROWING_FLAGS:
            return False
        if token.endswith(".py"):
            return False
        if token == "-m" and (tokens[i + 1] if i + 1 < len(tokens) else "") != "pytest":
            return False
    return True


def is_pytest_evidence(command: str | None, result: str | None) -> bool:
    """该次 terminal 调用能否作为"全量测试通过"的证据。

    三条都要满足:
      1. 命令以全量 pytest 调用收尾(见上);
      2. **确实执行过**——返回串带 "[SHELL:" 标记。
         这一条是必需的:用户按 n 取消时 handler 返回 "[用户取消] 命令未执行",
         它既不含 [SHELL: 也不含任何错误特征,不加这条会被误记为"通过"。
      3. 返回串无错误特征(失败时 terminal 会附 "[EXIT CODE: N]")。
    """
    text = result or ""
    return (
        is_full_suite_pytest_command(command)
        and _TERMINAL_EXECUTED_MARKER in text
        and not is_tool_error(text)
    )


def is_evidence_command(tool_name: str, args: dict[str, Any] | None) -> bool:
    """这次调用有没有可能产出验证证据(即"一次全量 pytest 运行")。

    护栏拦掉一次调用时,调用方用它判断"拦掉的是不是取证路径":
    是的话本轮已经拿不到证据了,收尾门不该再逼模型去跑一条被禁止的命令。
    """
    return (tool_name == "terminal"
            and is_full_suite_pytest_command((args or {}).get("command")))


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
    _evidence_blocked: bool = False
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
        self._evidence_blocked = False
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
            if is_pytest_evidence(command, result):
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

    def note_blocked(self, tool_name: str, args: dict[str, Any] | None) -> None:
        """护栏拦掉了一次调用。若它本来是取证路径(全量 pytest),记为"取证被阻断"。

        为什么要区分:收尾门驳回的前提是"模型本来能验证却没验证"。而护栏
        把那条命令禁掉时,模型**怎么都拿不到证据**——再驳回就是逼它去执行
        一条被禁止的命令(实测结局:驳回 2 次后以"无证据 + 声称完成"收尾,
        正是这道门要防的东西)。被阻断属外部原因,门应当放行。
        """
        if is_evidence_command(tool_name, args):
            self._evidence_blocked = True

    def build_stop_nudge(self) -> str | None:
        """收尾前调用:该驳回就返回驳回文本并计一次;否则返回 None(放行)。"""
        if not self.enabled or self._attempts >= self.max_attempts:
            return None
        if self._evidence_blocked:
            # 取证路径被护栏阻断(见 note_blocked):本轮已无法验证,放行。
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

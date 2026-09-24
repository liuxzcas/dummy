"""terminal 工具的附加功能测试（LLM 生成命令之外的附加行为）。

============ 为什么补这个文件（2026-09-18） ============
用户提问："terminal 工具在执行 LLM 生成的指令之外都有哪些额外的功能？都验证过了吗？"

逐项核对后发现三个功能**有实现、手工验证通过、但没有测试覆盖**：

    _find_bash()      shell 选择逻辑（含"必须用绝对路径避开 WSL"这条）
    确认 / 取消        _confirm 分支（弹面板等回车 / 按 n 取消）
    _command_hint()   命令提示映射（ls→📂 / rm→🗑️ 警告）

其中 **_command_hint 是安全相关的** —— 它对 rm/del 显示
"删除文件/目录(不可恢复,请谨慎)"。如果它坏了不会报错，
只是用户看不到警告 —— 这种静默失效最难发现。

同时删除了一处死代码（见 test_no_dead_branch_for_empty_output）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.terminal import (                                   # noqa: E402
    COMMAND_HINTS, _command_hint, _find_bash, terminal_handler,
)


# ============================================================
# _find_bash：shell 选择
# ============================================================
def test_find_bash_returns_absolute_path():
    """必须返回绝对路径（或 None）—— 这是避开 WSL 劫持的关键。

    实测依据(2026-09-18):让子进程执行裸 "bash" 会被 WSL 拦截 ——
        shutil.which("bash") 找到 hermes 自带的 git,
        但 subprocess.run(["bash", ...]) 报
          "WSL (25 - Relay) ERROR: execvpe(/bin/bash) failed"
    原因是 Windows 把裸命令名交给 WSL 启动器。绝对路径可绕开。
    """
    b = _find_bash()
    assert b is None or os.path.isabs(b), f"_find_bash() 返回了相对路径: {b!r}"


def test_find_bash_prefers_configured_path(monkeypatch, tmp_path):
    """DUMMY_BASH_PATH 环境变量优先（手动配置项，不做自动探测）。"""
    fake = tmp_path / "fake_bash"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DUMMY_BASH_PATH", str(fake))
    assert _find_bash() == str(fake)


def test_find_bash_ignores_nonexistent_configured_path(monkeypatch):
    """配置的路径不存在时，不能返回它（否则调用方拿到坏路径）。"""
    monkeypatch.setenv("DUMMY_BASH_PATH", r"C:\definitely\not\here\bash.exe")
    b = _find_bash()
    assert b != r"C:\definitely\not\here\bash.exe"


# ============================================================
# _command_hint：命令提示（安全相关）
# ============================================================
def test_command_hint_covers_listing_commands():
    """列目录类命令有提示。"""
    for cmd in ("ls -la", "ls", "dir", "tree", "ll"):
        hint = _command_hint(cmd)
        assert hint is not None, f"{cmd} 应该有提示"
        assert "目录" in hint


def test_command_hint_covers_search_commands():
    """搜索类命令有提示。"""
    for cmd in ("grep pattern", "rg x", "findstr", "find ."):
        hint = _command_hint(cmd)
        assert hint is not None, f"{cmd} 应该有提示"
        assert "搜索" in hint


def test_command_hint_warns_on_destructive_commands():
    """**删除类命令必须有警告提示** —— 这是安全相关的。

    如果这条坏了（例如 COMMAND_HINTS 被误改），用户不会收到
    "不可恢复，请谨慎"的警告，而系统不会有任何报错。
    """
    for cmd in ("rm -rf /tmp/x", "del file.txt", "erase x", "rmdir somedir"):
        hint = _command_hint(cmd)
        assert hint is not None, f"{cmd} 应该有警告提示"
        assert "删除" in hint, f"{cmd} 的提示应说明是删除操作: {hint}"
        assert "谨慎" in hint, f"{cmd} 的提示应提醒谨慎: {hint}"


def test_command_hint_is_case_insensitive():
    """大小写不敏感（LLM 可能写 LS / RM）。"""
    assert _command_hint("LS -la") == _command_hint("ls -la")
    assert _command_hint("RM -rf x") == _command_hint("rm -rf x")


def test_command_hint_handles_whitespace():
    """前后空白不影响识别。"""
    assert _command_hint("  ls  ") == _command_hint("ls")
    assert _command_hint("\tls -la\n") == _command_hint("ls -la")


def test_command_hint_returns_none_for_unknown():
    """未知命令返回 None（不编造提示）。"""
    for cmd in ("python foo.py", "git status", "npm install", ""):
        assert _command_hint(cmd) is None, f"{cmd!r} 不该有提示"


def test_command_hint_returns_none_for_blank():
    """纯空白输入不能崩，返回 None。"""
    assert _command_hint("") is None
    assert _command_hint("   ") is None
    assert _command_hint("\n\t") is None


def test_command_hint_prefix_does_not_false_match():
    """不能因为前缀相同就误判（例如 "lsomething" 不该被当成 ls）。

    当前实现取的是第一个空格分隔的 token 做精确匹配,
    所以 "lsomething" 不在列表里 → 返回 None。
    """
    assert _command_hint("lsomething") is None
    assert _command_hint("rmdirx") is None


def test_command_hints_table_is_well_formed():
    """表结构正确（每组 3 个元素：命令元组 + emoji + 说明）。"""
    assert len(COMMAND_HINTS) >= 3
    for entry in COMMAND_HINTS:
        assert len(entry) == 3, f"条目格式错误: {entry}"
        names, emoji, desc = entry
        assert isinstance(names, tuple) and names, f"命令名必须是非空元组: {entry}"
        assert isinstance(desc, str) and desc, f"说明必须是非空字符串: {entry}"


# ============================================================
# 确认 / 取消
# ============================================================
def test_confirm_is_called_before_execution():
    """注入 _confirm 时，执行前会调用它（弹确认面板）。"""
    prompts = []
    out = terminal_handler("echo confirmed_ok",
                           _confirm=lambda p: prompts.append(p) or "")
    assert len(prompts) == 1, "确认函数应被调用一次"
    assert "确认" in prompts[0], f"提示文本应含'确认': {prompts[0]!r}"
    assert "confirmed_ok" in out


def test_cancel_prevents_execution():
    """用户输入 n → 命令不执行，返回取消标记。"""
    out = terminal_handler("echo SHOULD_NOT_APPEAR",
                           _confirm=lambda p: "n")
    assert "[用户取消]" in out
    assert "SHOULD_NOT_APPEAR" not in out, "取消后命令不该执行"
    assert "[EXIT CODE" not in out


def test_cancel_is_case_insensitive_and_trimmed():
    """取消判定对大小写和空白宽容。"""
    for answer in ("n", "N", " n ", "\tn\n"):
        out = terminal_handler("echo NOPE", _confirm=lambda p, a=answer: a)
        assert "[用户取消]" in out, f"输入 {answer!r} 应被识别为取消"


def test_empty_answer_means_confirm():
    """直接回车（空串）→ 确认执行。"""
    out = terminal_handler("echo went_through", _confirm=lambda p: "")
    assert "went_through" in out
    assert "[用户取消]" not in out


def test_any_other_answer_means_confirm():
    """非 n 的输入（如 y、随便打）→ 都视为确认（只有 n 是取消）。"""
    out = terminal_handler("echo ran_anyway", _confirm=lambda p: "y")
    assert "ran_anyway" in out


def test_confirm_none_skips_prompt():
    """_confirm=None（直接调用/测试）→ 跳过确认、直接执行。"""
    out = terminal_handler("echo no_prompt")
    assert "no_prompt" in out
    assert "[用户取消]" not in out


def test_cancel_does_not_register_task():
    """取消时不应留下登记记录（命令根本没跑）。"""
    from tools.tasks import tasks
    n_before = len(tasks.all_tasks())
    terminal_handler("echo cancelled", _confirm=lambda p: "n")
    n_after = len(tasks.all_tasks())
    assert n_after == n_before, "取消的命令不该被登记"


# ============================================================
# 死代码清理的回归
# ============================================================
def test_no_dead_branch_for_empty_output():
    """空输出命令的返回值 = 环境声明（没有"无输出"特殊分支）。

    历史上这里有 `... if output_parts else "(命令执行成功，无输出)"`,
    但自 [SHELL:] 声明加入后 output_parts 永远非空,那个分支是死代码。
    2026-09-18 删除。这条测试锁住"删除后行为不变"。

    判据说明:空输出命令的返回值应该**只有环境声明那一行**。
    如果将来有人把"(命令执行成功，无输出)"加回来,这条测试会失败。
    """
    out = terminal_handler("true")
    assert out.startswith("[SHELL:"), f"应以环境声明开头: {out!r}"
    assert "\n" not in out, f"空输出命令应只有一行(环境声明),实际多行: {out!r}"
    assert "命令执行成功" not in out, "不该出现'命令执行成功'字样(那是已删的死分支)"

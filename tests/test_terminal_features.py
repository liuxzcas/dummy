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


# ============================================================
# 子进程不能继承控制台 stdin（/p 打断，场景 A）
# ============================================================
def test_subprocess_stdin_is_devnull():
    """工具执行期间 /p 无反应 —— 根因是子进程继承了控制台。

    用户实测(2026-11-02):
        场景 A(工具执行中):敲 /p 完全无反应,只在本轮结束后才作为
                            下一轮输入出现
        场景 B(确认提示时):/p 正常
    → 差别是"有没有子进程在跑"。

    修法:Popen 显式给 stdin=DEVNULL,断开与控制台的继承关系,
         让 _InterruptListener 的 msvcrt 轮询能读到键。

    这条测试用 AST 检查源码 —— 直接跑命令测不到 stdin 继承关系。
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "tools" / "terminal.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        # 找 kwargs = {"stdin": ..., ...}
        if not (isinstance(node.value, ast.Dict) and node.value.keys):
            continue
        keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
        if "stdout" in keys and "stderr" in keys:
            found = True
            assert "stdin" in keys, (
                "terminal 的 Popen kwargs 必须显式给 stdin —— "
                "否则子进程继承控制台，/p 打断在工具执行期间失效"
            )
            # 值必须是 DEVNULL
            idx = keys.index("stdin")
            val = node.value.values[idx]
            assert isinstance(val, ast.Attribute) and val.attr == "DEVNULL", (
                f"stdin 应为 subprocess.DEVNULL，实际是 "
                f"{ast.unparse(val)}"
            )
            break

    assert found, "没找到 Popen 的 kwargs 定义（代码结构变了？）"






# ============================================================
# /p 输入回显（用户 2026-11-02 两次实测反馈）
# ============================================================
class TestInterruptEcho:
    """用户两次反馈:

    第一次:"检测到了 /p,并且做了正确处理。但是我们屏幕上看不到输入了 /p"
    第二次:"可以看到正在输入打断指令,但'已收到'的处理不太好,
            在打断指令开始后打字依然静默。"

    ============ 最终方案:逐字符回显 ============
    演进:
      ① 完全静默(原始设计,"像密码输入")      → 用户盲打,不知道敲了什么
      ② 首字符提示 + 回车确认(/p 打错看不见)  → 输入过程仍盲打
      ③ **逐字符回显 + 退格处理**             ← 现在

    为什么终于敢用逐字符回显:
      原来担心"命令正在输出时字符会和输出混在一起",查了 terminal 工具
      的实现才确认**不成立** —— 它用读线程把输出攒在 buf 里,
      命令结束后才一次性打印,执行期间终端是空的。

    ============ 为什么用假 msvcrt 驱动(踩坑记录) ============
    初版测试只调提示函数本身、断言"它打了字" —— **无效**:
    把 _poll_loop 里的调用整段删掉,测试仍然全绿。
    这类"测函数、不测接线"的断言等于没测。
    正确做法:用假 kbhit/getwch 喂字符驱动真实 _poll_loop,
              断言它**确实**按预期回显。
    """

    class FakeMsvcrt:
        def __init__(self, chars):
            self._q = list(chars)

        def kbhit(self):
            return bool(self._q)

        def getwch(self):
            return self._q.pop(0)

    def _run(self, chars, monkeypatch):
        """驱动真实 _poll_loop,返回 (buffer, 屏幕输出)。"""
        import threading
        import time as _time
        import core

        out = []
        monkeypatch.setattr(
            core._InterruptListener, "_echo_raw",
            staticmethod(lambda t: out.append(t)))

        l = core._InterruptListener.__new__(core._InterruptListener)
        l._buffer = ""
        l._paused = False
        l._paused_seen = threading.Event()
        l._stop = False
        l._lock = threading.Lock()
        l._msvcrt = self.FakeMsvcrt(chars)

        orig_sleep = _time.sleep
        calls = {"n": 0}

        def fake_sleep(_):
            calls["n"] += 1
            if calls["n"] > 200 or not l._msvcrt._q:
                l._stop = True
            orig_sleep(0.001)

        monkeypatch.setattr(_time, "sleep", fake_sleep)
        l._poll_loop()
        return l._buffer, "".join(out)

    # ---- 核心需求:打字要看得见 ----

    def test_each_char_echoed(self, monkeypatch):
        """敲每个字符都要立刻出现在屏幕上(用户核心诉求)。

        ★ 断言写法(2026-11-02 踩坑):提示语里本身含 "/p" 字样,
          写 `"/p" in out` 会**永远通过** —— 即使回显整段被删掉。
          实测:去掉逐字符回显后,这条仍然绿。
          改成断言"输入字符出现在提示之后"(提示是首字符时打的,
          真回显必须排在它后面)。
        """
        _buf, out = self._run(list("/p") + [chr(13)], monkeypatch)
        after_prompt = out.split("建议输入 /p)] ", 1)[-1]
        assert after_prompt.startswith("/p"), (
            f"提示之后应紧跟回显的 /p,实际: {after_prompt!r}")

    def test_chars_echoed_as_typed(self, monkeypatch):
        """逐字符:敲 / 就有 /,敲 p 就有 p(不是等回车才一起出现)。"""
        _buf, out = self._run(["/", "p", "x"] + [chr(13)], monkeypatch)
        # 回显部分应包含连续的用户输入
        assert "/px" in out.replace(" ", ""), f"应逐字符回显: {out!r}"

    def test_prompt_shown_once_at_start(self, monkeypatch):
        """开头的说明只打一次,不每敲一个字符打一次。"""
        _buf, out = self._run(list("/p") + [chr(13)], monkeypatch)
        assert out.count("正在输入打断指令") == 1, out

    def test_prompt_precedes_input(self, monkeypatch):
        """说明要在输入之前(用户先知道自己在干什么)。"""
        _buf, out = self._run(list("/p") + [chr(13)], monkeypatch)
        assert out.index("正在输入打断指令") < out.index("/p")

    # ---- 退格 ----

    def test_backspace_erases_on_screen(self, monkeypatch):
        """退格要擦掉屏幕上一个字符(\b 空格 \b)。"""
        _buf, out = self._run(["/", "q", chr(8), "p"] + [chr(13)], monkeypatch)
        assert "\b \b" in out, f"退格没有擦屏: {out!r}"
        assert _buf == "/p" + chr(10), f"buffer 应被退格修正: {_buf!r}"

    def test_delete_key_also_erases(self, monkeypatch):
        """DEL(127) 和退格(8) 一样处理。"""
        _buf, out = self._run(["/", "q", chr(127), "p"] + [chr(13)], monkeypatch)
        assert "\b \b" in out
        assert _buf == "/p" + chr(10)

    # ---- 边界 ----

    def test_no_output_without_input(self, monkeypatch):
        """没有输入时不该有任何回显。"""
        _buf, out = self._run([], monkeypatch)
        assert out == "", f"不该有输出: {out!r}"

    def test_enter_adds_newline(self, monkeypatch):
        """回车要补一个换行(否则后续输出接在同一行)。"""
        _buf, out = self._run(list("/p") + [chr(13)], monkeypatch)
        assert out.endswith("\n"), f"回车后应换行: {out!r}"

    def test_second_line_gets_fresh_prompt(self, monkeypatch):
        """连打两行:每行开头都要有自己的说明。"""
        _buf, out = self._run(
            list("/p") + [chr(13)] + list("/p x") + [chr(13)], monkeypatch)
        assert out.count("正在输入打断指令") == 2, out

    def test_buffer_unchanged_by_echo(self, monkeypatch):
        """回显不能影响 buffer(take_line 的数据源)。"""
        buf, _ = self._run(list("/p") + [chr(13)], monkeypatch)
        assert buf == "/p" + chr(10)

    def test_echo_failure_does_not_break_polling(self, monkeypatch):
        """回显写失败(stdout 关闭)不能中断打断线程。"""
        import threading
        import time as _time
        import core

        def boom(_t):
            raise OSError("closed")

        monkeypatch.setattr(core._InterruptListener, "_echo_raw", staticmethod(boom))

        l = core._InterruptListener.__new__(core._InterruptListener)
        l._buffer = ""
        l._paused = False
        l._paused_seen = threading.Event()
        l._stop = False
        l._lock = threading.Lock()
        l._msvcrt = self.FakeMsvcrt(list("/p") + [chr(13)])

        orig_sleep = _time.sleep
        calls = {"n": 0}

        def fake_sleep(_):
            calls["n"] += 1
            if calls["n"] > 200 or not l._msvcrt._q:
                l._stop = True
            orig_sleep(0.001)

        monkeypatch.setattr(_time, "sleep", fake_sleep)
        l._poll_loop()          # 不该抛
        assert l._buffer == "/p" + chr(10)


# ============================================================
# 跨调用的 cwd 保持（2026-11-05 实现）
# ============================================================
class TestCwdPersistence:
    """用户日志分析驱动:terminal 130 次调用里 117 次以 cd 开头。

    ============ 根因 ============
    每次 terminal 调用都是**新的 bash 子进程**,上一次的 cd 不会保留,
    所以模型每次都要重新 cd 到目标目录 —— 白白烧掉轮次和 token。
    cd 目标 Top3:
        ×55  cd /d/ja/sjcj                    ← 用户项目目录
        ×29  cd .../skills/.../templates
        ×10  cd .../garage/vt2

    ============ 方案(借 Hermes 的做法) ============
    命令末尾追加 `pwd -P > <临时文件>`,执行完读回来更新会话 cwd。
    用文件而不是 stdout 传值 —— 模型看不到那一行,输出保持干净。

    ============ 两个坑(Hermes 代码里都有血泪) ============
    ① Windows 路径:git-bash 的 pwd -P 输出 /c/Users/x,
       Popen(cwd=...) 需要 C:/Users/x —— 不翻译 isdir 必失败。
    ② cwd 被删:模型 `rm -rf` 掉自己的 cwd 后,
       下次 Popen(cwd=不存在) 抛 FileNotFoundError,整个工具报废。
       (Hermes issue #17558)
    """

    def test_msys_to_native(self):
        import tools.terminal as T
        assert T._msys_to_native("/c/Users/x") == r"C:\Users\x"
        assert T._msys_to_native("/d/Engineering") == r"D:\Engineering"
        assert T._msys_to_native("/cygdrive/e/foo") == r"E:\foo"

    def test_msys_to_native_keeps_native(self):
        import tools.terminal as T
        assert T._msys_to_native("C:/already/native") == "C:/already/native"

    def test_safe_cwd_falls_back_when_deleted(self, tmp_path, monkeypatch):
        """cwd 被删 → 逐级上溯到存在的父目录,最终兜底 garage。"""
        import tools.terminal as T
        import workspace

        gone = tmp_path / "gone" / "deeper"
        ghost = str(gone)
        monkeypatch.setattr(workspace, "ensure", lambda: str(tmp_path))
        got = T._safe_cwd(ghost)
        assert os.path.isdir(got), got
        assert got == str(tmp_path)   # 上溯到 tmp_path(存在)

    def test_marked_command_appends_pwd(self):
        """git-bash 下要追加 pwd -P 到临时文件。"""
        import tools.terminal as T
        cmd = T._marked_command("ls", "git-bash")
        assert "pwd -P" in cmd
        assert "dummy-terminal-cwd" in cmd
        assert cmd.startswith("ls")

    def test_marked_command_does_not_leak_to_stdout(self):
        """用文件传值,不能在命令里 echo 出来。"""
        import tools.terminal as T
        cmd = T._marked_command("echo hi", "git-bash")
        assert "echo $" not in cmd and "printf" not in cmd

    def test_session_cwd_actually_persists(self, tmp_path, monkeypatch):
        """端到端:cd 一次后,下一条命令自动在新目录。"""
        import tools.terminal as T
        from tools import create_default_registry

        target = tmp_path / "sub"
        target.mkdir()
        monkeypatch.setattr(T, "_session_cwd", None)

        reg = create_default_registry()
        reg.set_confirm_provider(lambda p: "")

        reg.dispatch("terminal", {"command": f'cd "{target}"'})
        assert T._session_cwd == str(target), T._session_cwd

        # 下一条命令不写 cd,应该已经在 target 里
        reg.dispatch("terminal", {"command": "touch marker.txt"})
        assert (target / "marker.txt").exists(), "cwd 没有保持"


class TestCwdPersistenceExitCode:
    """追加 pwd 记录不能吞掉原命令的退出码(2026-11-05 实测)。

    ============ 踩到的真 bug ============
    最初直接拼 `pwd -P > file || true` —— 整个脚本的退出码变成最后
    一条命令(0),原命令的失败被吞掉:

        definitely-not-a-command-xyz → 原本 rc=127
        拼上 || true 之后           → rc=0

    后果两层:
      ① tests/test_self_improve.py::test_dispatch_stats 直接红
      ② **模型也会以为命令成功了** —— 比测试红更严重
    """

    def test_rc_preserved_on_failure(self):
        import tools.terminal as T
        from tools import create_default_registry
        reg = create_default_registry()
        reg.set_confirm_provider(lambda p: "")
        out = reg.dispatch("terminal",
                           {"command": "definitely-not-a-command-xyz"})
        assert "EXIT CODE: 127" in out, f"失败退出码被吞了: {out[-200:]}"

    def test_rc_zero_on_success(self):
        import tools.terminal as T
        from tools import create_default_registry
        reg = create_default_registry()
        reg.set_confirm_provider(lambda p: "")
        out = reg.dispatch("terminal", {"command": "echo hi"})
        assert "EXIT CODE" not in out, f"成功不该报退出码: {out[-200:]}"

    def test_marked_command_saves_and_restores_rc(self):
        import tools.terminal as T
        cmd = T._marked_command("false", "git-bash")
        assert "$?" in cmd, "必须先存 $?"
        assert "exit" in cmd, "必须还原退出码"


class TestMsysToNativeRobustness:
    """/tmp 映射与 /cygdrive 前缀(2026-11-05 实测踩到)。"""

    def test_tmp_maps_to_windows_temp(self):
        """/tmp 在 git-bash 里是独立映射,不在任何盘符下。

        不处理的话 cwd 会永远保持不住(且只在 pwd 落在 /tmp 时复现)。
        """
        import tools.terminal as T
        got = T._msys_to_native("/tmp/x")
        assert os.name != "nt" or os.path.isabs(got) and got != "/tmp/x", got

    def test_cygdrive_prefix_handled(self):
        """/cygdrive/e/foo 要变成 E:\foo。

        踩坑:cygpath 会把它当相对路径转成
        C:\...\hermes\git\cygdrive\e\foo —— 必须剥前缀再转。
        """
        import tools.terminal as T
        assert T._msys_to_native("/cygdrive/e/foo") == r"E:\foo"

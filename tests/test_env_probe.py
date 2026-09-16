"""env_probe 测试 —— 跨平台环境编码探测。

============ 为什么必须有测试 ============
这套探测的意义在于**跨平台**:同一份代码在 Windows/Linux 上都要给出
真实值,而不是"在 Windows 上写死的解法"。

2026-09-16 实测背景:一次任务 18 次工具调用里 11 次在跟编码搏斗
(模型不知道控制台是什么代码页,只能反复探测 + A/B 对照)。
本模块把这些探测**前置到工具返回里**,模型看一眼就知道。

关键设计约束(用户明确指出):**探测,不设置**。
绝不能在这里设 LANG / 执行 chcp / 改 stdout —— 那会把当前平台的解法
写死进代码,换系统就是错的。
"""
import os
import sys

import env_probe
from env_probe import (
    describe_environment,
    _console_encoding,
    _fs_encoding,
    _python_io_encoding,
)


# ============================================================
# 声明格式
# ============================================================
def test_describe_format_is_parseable():
    """首行必须是 `[SHELL: xxx | os=... | console=... | py_io=... | fs=...]`。

    格式稳定很重要:模型靠它读取环境,也对齐既有的 `[SHELL:` 标记约定
    (lessons/turn_context 等模块按这个前缀识别工具返回)。
    """
    line = describe_environment("git-bash")
    assert line.startswith("[SHELL: ")
    assert line.endswith("]")
    for key in ("os=", "console=", "py_io=", "fs="):
        assert key in line, f"缺少 {key}: {line}"


def test_describe_includes_shell_name():
    assert "git-bash" in describe_environment("git-bash")
    assert "cmd" in describe_environment("cmd")


def test_describe_is_single_line():
    """必须单行 —— 多行会干扰模型对返回结构的解析。"""
    assert "\n" not in describe_environment("git-bash")


# ============================================================
# 探测结果合理性(不假设具体值,因为跨平台)
# ============================================================
def test_console_encoding_never_empty():
    """探不到要说 unknown,不能返回空串或崩掉。"""
    enc = _console_encoding()
    assert isinstance(enc, str) and enc, "必须返回非空字符串(探不到给 unknown)"


def test_console_encoding_matches_platform_shape():
    """各平台返回的形状要对。

    Windows → cp 形式的代码页(如 cp936),或 unknown
    POSIX   → 编码名(如 UTF-8),或 unknown
    """
    enc = _console_encoding()
    if os.name == "nt":
        assert enc == "unknown" or enc.startswith("cp"), \
            f"Windows 上应是 cpNNN 或 unknown,实际: {enc}"
    else:
        assert enc == "unknown" or enc.isprintable(), f"异常值: {enc}"


def test_python_io_encoding_present():
    """Python 进程 IO 编码(实测本机是 utf-8)。"""
    enc = _python_io_encoding()
    assert isinstance(enc, str) and enc


def test_fs_encoding_present():
    enc = _fs_encoding()
    assert isinstance(enc, str) and enc


# ============================================================
# 关键约束:只探测,不设置
# ============================================================
def test_does_not_mutate_environment():
    """**绝不能修改环境变量或进程状态** —— 这是本模块的设计红线。

    一旦在这里 setenv LANG / 执行 chcp / 改 stdout 编码,就把"当前平台的
    解法"写死了,换到别的系统就是错的(用户明确的约束)。
    """
    before = dict(os.environ)
    describe_environment("git-bash")
    after = dict(os.environ)
    assert before == after, "探测过程不得修改任何环境变量"


def test_does_not_change_stdout_encoding():
    """不得改动 sys.stdout 的编码。"""
    enc_before = getattr(sys.stdout, "encoding", None)
    describe_environment("git-bash")
    assert getattr(sys.stdout, "encoding", None) == enc_before


# ============================================================
# 容错:探测失败不能抛异常(否则整个 terminal 工具都挂)
# ============================================================
def test_survives_probe_failure(monkeypatch):
    """任一探测抛异常时,不得让 describe_environment 失败。"""
    def boom(*a, **k):
        raise RuntimeError("模拟探测失败")

    monkeypatch.setattr(env_probe, "locale", type("L", (), {
        "getpreferredencoding": staticmethod(boom)}))
    monkeypatch.setattr(env_probe.subprocess, "run", boom)
    # 不应抛异常
    line = describe_environment("git-bash")
    assert line.startswith("[SHELL: ")


def test_posix_branch_parses_locale(monkeypatch):
    """POSIX 分支:从 LC_ALL 里解析编码。

    **注意**:本机是 Windows,这条测试是 monkeypatch 走 POSIX 分支的
    **逻辑验证**,不是在真实 Linux 上跑的。真机验证状态见 env_probe 模块头。
    """
    monkeypatch.setattr(env_probe.os, "name", "posix")
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    assert env_probe._console_encoding() == "UTF-8"

    monkeypatch.setenv("LC_ALL", "zh_CN.GBK")
    assert env_probe._console_encoding() == "GBK"


def test_posix_branch_falls_back_when_all_unset(monkeypatch):
    """POSIX 上三个 locale 变量都没设时,走 locale 兜底而不是崩掉。"""
    monkeypatch.setattr(env_probe.os, "name", "posix")
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        monkeypatch.delenv(var, raising=False)
    enc = env_probe._console_encoding()
    assert isinstance(enc, str) and enc

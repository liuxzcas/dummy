"""环境编码探测(跨平台) —— 供 terminal 工具在返回结果首行声明。

============ 为什么要这个 ============
2026-09-16 实测(conversation_20260916_203636):18 次工具调用里 **11 次**
在跟编码搏斗 —— 模型不知道"这台机器的控制台是什么编码",于是反复:
  · 调 chcp 查代码页
  · 写 python -c "open(...,'rb').decode(...)" 探文件编码
  · 写临时文件做 A/B 对照

这些探测本身又要写文件、又要清理。根源是**环境信息缺失**。

============ 设计原则:探测,不设置 ============
**绝不**在这里设 LANG / 执行 chcp / 改 sys.stdout —— 那会把"当前平台的
解法"写死进代码,换到别的系统就是错的(用户明确指出的约束)。

只做一件事:**如实报告当前环境是什么**。探不到就报 unknown,不猜。

各平台各探各的,同一套逻辑:
  Windows  console_cp : chcp(或 "cp" + GetConsoleOutputCP)
  Linux/macOS        : locale / LANG 环境变量里的编码部分
  都失败             : "unknown"

============ 验证状态(诚实标注) ============
- Windows 分支:**已在本机实测**(返回 console=cp936,与实际 chcp 一致)
- POSIX 分支:**只做了逻辑验证**(monkeypatch 走该分支,喂各种 LANG 值),
  **没有在真实 Linux 机器上跑过**。原因是本机 WSL 不可用(已知问题)、
  Docker daemon 未启动(不擅自启动用户的 Docker Desktop)。
  首次在 Linux 部署时,请确认 console 字段与实际 `locale` 输出一致。
"""
from __future__ import annotations

import locale
import os
import subprocess
import sys


def _console_encoding() -> str:
    """当前**控制台/终端**用的编码(不是文件系统、不是 Python IO)。

    Windows: 从 `chcp` 输出解析代码页(如 "936" → cp936/GBK)。
    POSIX: 从 LC_ALL/LC_CTYPE/LANG 里取编码部分(如 "C.UTF-8" → UTF-8)。
    探不到返回 "unknown" —— 宁可说不知道,也不要给错的信息。
    """
    if os.name == "nt":
        try:
            # chcp 输出形如 "Active code page: 936" / "活动代码页: 936"
            out = subprocess.run(
                ["chcp"], capture_output=True, timeout=5,
                shell=True, text=True, errors="replace",
            ).stdout or ""
            digits = "".join(ch for ch in out if ch.isdigit())
            return f"cp{digits}" if digits else "unknown"
        except Exception:
            return "unknown"

    # POSIX:优先看 locale 环境变量(比 locale.getpreferredencoding 更贴近终端)
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        val = os.environ.get(var, "")
        if val and "." in val:
            enc = val.split(".", 1)[1].split("@", 1)[0]
            if enc:
                return enc
    try:
        enc = locale.getpreferredencoding(False)
        return enc or "unknown"
    except Exception:
        return "unknown"


def _python_io_encoding() -> str:
    """Python 进程 stdout 的编码(影响 `python -c "print(...)"` 这类输出)。"""
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", None)
        if enc:
            return enc
    return "unknown"


def _fs_encoding() -> str:
    """文件系统路径 + 源码默认编码(`sys.getdefaultencoding`)。"""
    try:
        return sys.getfilesystemencoding() or "unknown"
    except Exception:
        return "unknown"


def describe_environment(shell_name: str) -> str:
    """生成返回结果的首行环境声明。

    形如:
      [SHELL: git-bash | os=win32 | console=cp936 | py_io=utf-8 | fs=utf-8]

    模型据此可以立刻判断:
      · 控制台是 cp936(GBK) → 打印 UTF-8 中文会乱码 → 需要 chcp 65001
      · py_io 是 utf-8       → Python 输出中文没问题
      · fs 是 utf-8          → 文件名/路径按 UTF-8 处理
    **不必再自己探测。**
    """
    return (
        f"[SHELL: {shell_name} | os={sys.platform} | "
        f"console={_console_encoding()} | "
        f"py_io={_python_io_encoding()} | "
        f"fs={_fs_encoding()}]"
    )

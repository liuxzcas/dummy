"""
tools/read_file.py — read_file 工具实现

读取指定文件的内容，支持 offset/limit 分页。

=== 为什么需要翻页？===

这个翻页机制不是为了"让用户翻页"设计的，而是给 LLM 自己用的。

流程：
  用户说"看看 core.py"
  → LLM 决定调 read_file("core.py", limit=50)
  → 返回带行号的内容 + 页脚 "[行 1-50 / 共 316 行][继续读取: offset=51]"
  → LLM 看到页脚，知道还有 266 行没读
  → 按需再调 read_file("core.py", offset=51, limit=50)

好处：
- 小文件一次读完，大文件分批读，省 token
- LLM 自己控制"这次读多少"，而不是一次把整个大文件塞进上下文

注意：终端上用户看到的预览被 core.py 第 262 行的 300 字符截断限制，
但回注给 LLM 的是完整结果。翻页机制在 LLM 侧工作，不在终端侧。

=== 确认机制（2026-08-14 定稿） ===

文件内容会随工具结果回注给 LLM(在线 API)，读取前必须让用户知情。
确认交互在 handler 内（路径 + 隐私提示），输入收集与 '/p' 拦截由
core 注入的 _confirm 函数统一处理。_confirm 为 None 时跳过确认。
"""

import os

from colors import paint, CYAN, YELLOW


def read_file_handler(path: str, offset: int = 1, limit: int | None = None, _confirm=None) -> str:
    """读取文件内容并返回。

    参数：
    - path: 文件路径
    - offset: 起始行号（1-indexed，默认 1）
    - limit: 最大行数（默认 None，表示全部）
    - _confirm: core 注入的确认函数(输入收集 + /p 拦截);None 时跳过确认
    """
    # ---- 读取前确认 ----
    if _confirm is not None:
        print(f"\n  {paint('📖 即将读取文件', CYAN)}: {path}")
        print(f"     {paint('⚠️ 隐私提示', YELLOW)}: 文件内容将发送给在线模型(LLM API),请确认不含敏感信息。")
        choice = _confirm("     按 Enter 允许读取, 输入 n 取消: ").strip().lower()
        if choice == "n":
            return "[用户取消] 文件未读取"

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return f"[错误] 文件不存在: {path}"
    except IsADirectoryError:
        return f"[错误] 路径是一个目录: {path}"
    except PermissionError:
        # Windows 上 open() 一个目录抛出 PermissionError
        if os.path.isdir(path):
            return f"[错误] 路径是一个目录: {path}"
        return f"[错误] 无权限读取: {path}"
    except UnicodeDecodeError:
        return f"[错误] 文件不是 UTF-8 编码的文本文件: {path}"
    except Exception as e:
        return f"[错误] 读取失败: {type(e).__name__}: {e}"

    total = len(lines)

    # 校验 offset
    if offset < 1:
        return f"[错误] offset 必须 >= 1（收到 {offset}）"
    if offset > total:
        return (
            f"[错误] offset 超出文件范围：文件共 {total} 行，offset={offset}"
        )

    # 切片
    start = offset - 1
    if limit is not None:
        end = start + limit
    else:
        end = total

    selected = lines[start:end]

    # 带行号输出
    output_lines = [
        f"{i}|{line.rstrip()}"
        for i, line in enumerate(selected, start=offset)
    ]

    # 附加页脚信息
    shown = len(selected)
    footer_parts = []
    if shown < total:
        footer_parts.append(f"[行 {offset}-{offset + shown - 1} / 共 {total} 行]")
    if offset > 1 or (limit and offset + limit - 1 < total):
        footer_parts.append(f"[继续读取: offset={offset + shown}]")
    footer = "\n".join(footer_parts)

    body = "\n".join(output_lines)
    return f"{body}\n{footer}" if footer else body

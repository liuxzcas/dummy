"""
tools/write_file.py — write_file 工具实现

写入内容到指定文件，自动创建父目录。

=== 路径安全 ===

项目根目录内：无限制自动写入。
项目根目录外：需要用户手动确认，防止 Agent 误操作写到系统关键路径。

=== Diff 预览（覆盖已有文件时） ===

覆盖已有文件时，自动对比新旧内容并展示差异供用户确认。
这是防止误覆盖的关键安全环节。

=== 原子写入 ===

先写入 .tmp 文件再 rename 到目标路径。
如果写入中途进程崩溃，不会留下半截文件。
"""

import os
import difflib


def _show_diff(old: str, new: str, path: str) -> bool:
    """展示新旧内容差异，询问用户是否确认。返回 True 表示确认写入。"""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)

    if old_lines == new_lines:
        print(f"  文件内容无变化，跳过写入: {path}")
        return False

    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile="旧", tofile="新",
        n=2,  # 每个差异块上下各 2 行上下文
    ))

    # 展示 diff（截断显示，太长的话只显示头尾）
    MAX_DIFF_LINES = 40
    if len(diff) > MAX_DIFF_LINES:
        for line in diff[:MAX_DIFF_LINES // 2]:
            print(f"  {line.rstrip()}")
        print(f"  ... (省略 {len(diff) - MAX_DIFF_LINES} 行)")
        for line in diff[-MAX_DIFF_LINES // 2:]:
            print(f"  {line.rstrip()}")
    else:
        for line in diff:
            print(f"  {line.rstrip()}")
            
    # 统计变更量
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))            
    print(f"\n📝 write_file 将覆盖: {path}")
    print(f"  旧: {len(old_lines)} 行 → 新: {len(new_lines)} 行 (+{added}/-{removed})")
    print()
    choice = input("  输入 y 确认写入 / n 拒绝 / d 查看完整 diff: ").strip().lower()
    if choice == "d":
        # 显示完整 diff（通过分页）
        for line in diff:
            
            print(f"  {line.rstrip()}")
        print()
        choice = input("  输入 y 确认写入 / n 拒绝: ").strip().lower()

    return choice == "y"


def write_file_handler(path: str, content: str, mode: str = "overwrite") -> str:
    """写入内容到文件。自动创建父目录，路径安全受控。

    参数：
    - path: 文件路径
    - content: 要写入的内容
    - mode: "overwrite" — 覆盖写入（默认）；"append" — 追加到文件末尾
    """
    project_root = os.path.abspath(os.getcwd())
    full_path = os.path.abspath(path)

    # ---- 路径安全检测 ----
    if not full_path.startswith(project_root):
        print(f"   项目: {project_root}")
        print(f"\n⚠️  write_file 试图写到项目目录之外:")
        print(f"   目标: {full_path}")
        choice = input("  输入 y 确认写入 / n 拒绝: ").strip().lower()
        if choice != "y":
            return f"[用户拒绝将内容写到项目目录之外] 写入已取消: {path}"
        print()  # 空行，让后续输出不挤在一起

    # ---- 写入 ----
    try:
        # 自动创建父目录
        parent = os.path.dirname(full_path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)

        # 校验 mode
        if mode == "overwrite":
            action = "写入"
        elif mode == "append":
            action = "追加"
        else:
            return f"[错误] 不支持的 mode 参数: {mode}（可选: overwrite, append）"

        # ---- Diff 预览（仅覆盖已有文件时） ----
        if mode == "overwrite" and os.path.exists(full_path):
            with open(full_path, "r", encoding="utf-8") as f:
                old_content = f.read()
            if not _show_diff(old_content, content, path):
                return "[跳过] 文件内容无变化"

        # ---- 追加模式：合并原内容 ----
        if mode == "append" and os.path.exists(full_path):
            with open(full_path, "r", encoding="utf-8") as f:
                old_content = f.read()
            content = old_content + content

        # ---- 原子写入 ----
        # 先写临时文件，再 rename，确保不会留下半截文件
        tmp_path = full_path + ".hermes-tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, full_path)

        # 统计
        lines = content.count("\n") + 1
        chars = len(content)
        return f"{action}成功: {path} ({lines} 行, {chars} 字符)"

    except PermissionError:
        return f"[错误] 无权限写入: {path}"
    except IsADirectoryError:
        return f"[错误] 路径是一个目录: {path}"
    except OSError as e:
        return f"[错误] 写入失败: {type(e).__name__}: {e}"

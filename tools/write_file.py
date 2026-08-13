"""
tools/write_file.py — write_file 工具实现

写入内容到指定文件，自动创建父目录。

=== 路径安全 ===

项目根目录内：无限制自动写入。
项目根目录外：需要用户手动确认，防止 Agent 误操作写到系统关键路径。

=== Diff 预览（覆盖已有文件时） ===

覆盖已有文件时，自动对比新旧内容并展示差异供用户确认。
这是防止误覆盖的关键安全环节。

=== 确认机制（2026-08-14 定稿） ===

所有修改已有文件的动作（overwrite / line / append / 路径外）都要确认。
确认交互在 handler 内（diff/行级预览），输入收集与 '/p' 拦截由
core 注入的 _confirm 函数统一处理。_confirm 为 None 时跳过确认。
确认前置、零副作用：所有有副作用的操作严格排在确认之后，
打断（InterruptSignal）发生时没有需要回滚的状态。

=== 原子写入 ===

先写入 .tmp 文件再 rename 到目标路径。
如果写入中途进程崩溃，不会留下半截文件。
"""

import os
import difflib
import py_compile


# ---- 文件类型验证器注册表 ----
# 扩展方法：加一个验证函数，注册到这里即可
# 签名：validator(path: str) -> (ok: bool, message: str)
VERIFIERS: dict[str, callable] = {}


def _verify_python(path: str) -> tuple[bool, str]:
    """Python 语法检查：使用 py_compile 编译文件。"""
    try:
        py_compile.compile(path, doraise=True)
        return (True, "")
    except py_compile.PyCompileError as e:
        # 提取行号和错误信息
        msg = str(e)
        line_num = ""
        if hasattr(e, "lineno") and e.lineno:
            line_num = str(e.lineno)
        elif "line " in msg:
            import re
            m = re.search(r"line (\d+)", msg)
            if m:
                line_num = m.group(1)
        return (False, f"第{line_num}行: {msg}" if line_num else msg)


VERIFIERS[".py"] = _verify_python
# ---- 后续扩展 ----
# VERIFIERS[".html"] = _verify_html   # Phase 2+
# VERIFIERS[".json"] = _verify_json   # Phase 2+


def _show_diff(old: str, new: str, path: str, _confirm) -> bool:
    """展示新旧内容差异，询问用户是否确认。返回 True 表示确认写入。

    输入收集由 core 注入的 _confirm 处理(命中 /p 抛 InterruptSignal，
    不返回)；handler 只负责 y/n/d 语义判断。
    """
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
    choice = ""
    while choice not in ("y", "n", "d"):
        choice = _confirm("  输入 y 确认写入 / n 拒绝 / d 查看完整 diff: ").strip().lower()
    if choice == "d":
        # 显示完整 diff（通过分页）
        for line in diff:
            print(f"  {line.rstrip()}")
        print()
        choice = _confirm("  输入 y 确认写入 / n 拒绝: ").strip().lower()

    return choice == "y"


def write_file_handler(path: str, content: str, mode: str = "overwrite", verify: bool = True, line: int | None = None, line_end: int | None = None, _confirm=None) -> str:
    """写入内容到文件。自动创建父目录，路径安全受控。

    参数：
    - path: 文件路径
    - content: 要写入的内容
    - mode: "overwrite" — 覆盖写入（默认）；"append" — 追加到文件末尾；
            "line" — 替换指定行或行范围的内容（需传 line，可选 line_end）
    - verify: 是否验证文件内容。对 .py 文件自动做语法检查。
              分多次写入时，最后一次设 True，之前设 False。
    - line: 仅 mode="line" 时有效。指定要替换的起始行号（从 1 开始）。
    - line_end: 仅 mode="line" 时有效。指定要替换的结束行号（含两端，默认等于 line）。
    - _confirm: core 注入的确认函数(diff/行级/路径外确认用);None 时跳过确认
    """
    project_root = os.path.abspath(os.getcwd())
    full_path = os.path.abspath(path)

    # ---- 路径安全检测（确认由注入的 _confirm 收集输入） ----
    if not full_path.startswith(project_root):
        print(f"   项目: {project_root}")
        print(f"\n⚠️  write_file 试图写到项目目录之外:")
        print(f"   目标: {full_path}")
        choice = 0
        if _confirm is not None:
            while choice not in ("y", "n"):
                choice = _confirm("  输入 y 确认写入 / n 拒绝: ").strip().lower()
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
        elif mode == "line":
            action = "修改行"
        else:
            return f"[错误] 不支持的 mode 参数: {mode}（可选: overwrite, append, line）"

        # ---- Diff 预览（覆盖已有文件时；确认输入由 _confirm 收集） ----
        if mode == "overwrite" and os.path.exists(full_path):
            with open(full_path, "r", encoding="utf-8") as f:
                old_content = f.read()
            if _confirm is None:
                if old_content == content:
                    return "[跳过] 文件内容无变化"
            elif not _show_diff(old_content, content, path, _confirm):
                return "[跳过] 文件内容无变化"

        # ---- 追加模式：合并原内容 ----
        if mode == "append" and os.path.exists(full_path):
            with open(full_path, "r", encoding="utf-8") as f:
                old_content = f.read()
            # 追加确认(零副作用:确认通过才合并)
            if _confirm is not None:
                print(f"  write_file 追加模式: 将追加 {len(content)} 字符到 {path}")
                for nl in content.splitlines():
                    print(f"  + {nl}")
                choice = _confirm("  输入 y 确认追加 / n 拒绝: ").strip().lower()
                if choice != "y":
                    return "[用户拒绝] 追加已取消"
            content = old_content + content

        # ---- 行替换模式：替换指定行或行范围 ----
        if mode == "line":
            if line is None:
                return "[错误] mode='line' 时必须指定 line 参数"
            if not os.path.exists(full_path):
                return f"[错误] 文件不存在，无法修改行: {path}"
            with open(full_path, "r", encoding="utf-8") as f:
                old_lines = f.readlines()
            if line < 1 or line > len(old_lines):
                return f"[错误] 行号越界: 文件共 {len(old_lines)} 行，起始行 {line}"
            end = line_end if line_end is not None else line
            if end < line or end > len(old_lines):
                return f"[错误] 行号越界: 文件共 {len(old_lines)} 行，结束行 {end}"
            old_content = "".join(old_lines)
            # 将新内容按行分割
            new_lines = content.splitlines(keepends=True)
            if not new_lines or not new_lines[-1].endswith("\n"):
                new_lines[-1] = new_lines[-1] + "\n"
            old_range = old_lines[line - 1:end]
            old_text = "".join(old_range)
            new_text = "".join(new_lines)
            # ---- 行级确认（替换前，零副作用） ----
            if _confirm is not None:
                print(f"  write_file line 模式: 修改第{line}~{end}行 ({len(old_range)}行 → {len(new_lines)}行)")
                for ol in old_range:
                    print(f"  - {ol.rstrip()}")
                for nl in new_lines:
                    print(f"  + {nl.rstrip()}")
                choice = _confirm("  输入 y 确认修改 / n 拒绝: ").strip().lower()
                if choice != "y":
                    return "[用户拒绝] 行修改已取消"
            # 替换范围
            old_lines[line - 1:end] = new_lines
            content = "".join(old_lines)
            # 展示修改的差异
            print(f"  write_file line 模式: 修改第{line}~{end}行 ({len(old_range)}行 → {len(new_lines)}行)")
            for ol, nl in zip(old_range, new_lines):
                if ol != nl:
                    print(f"  - {ol.rstrip()}")
                    print(f"  + {nl.rstrip()}")
                    break
            if len(old_range) != len(new_lines):
                print(f"  (共替换 {len(old_range)} 行 → {len(new_lines)} 行)")

        # ---- 原子写入 ----
        # 先写临时文件，再 rename，确保不会留下半截文件
        tmp_path = full_path + ".hermes-tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, full_path)

        # 统计
        lines = content.count("\n") + 1
        chars = len(content)
        result = f"{action}成功: {path} ({lines} 行, {chars} 字符)"

        # ---- 文件验证（仅 verify=True 且有对应验证器时） ----
        if verify:
            ext = os.path.splitext(full_path)[1].lower()
            verifier = VERIFIERS.get(ext)
            if verifier is not None:
                v_ok, v_msg = verifier(full_path)
                if not v_ok:
                    result += f"\n⚠️  语法检查未通过:\n{v_msg}"
        return result

    except PermissionError:
        return f"[错误] 无权限写入: {path}"
    except IsADirectoryError:
        return f"[错误] 路径是一个目录: {path}"
    except OSError as e:
        return f"[错误] 写入失败: {type(e).__name__}: {e}"

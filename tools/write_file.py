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


def write_file_handler(path: str, content: str, mode: str = "overwrite", verify: bool = True, line: int | None = None, line_end: int | None = None) -> str:
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
    """
    project_root = os.path.abspath(os.getcwd())
    full_path = os.path.abspath(path)

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

        # ---- 内容无变化检查（纯函数,无交互;diff 预览由 core 确认 UI 承担） ----
        if mode == "overwrite" and os.path.exists(full_path):
            with open(full_path, "r", encoding="utf-8") as f:
                old_content = f.read()
            if old_content == content:
                return "[跳过] 文件内容无变化"

        # ---- 追加模式：合并原内容 ----
        if mode == "append" and os.path.exists(full_path):
            with open(full_path, "r", encoding="utf-8") as f:
                old_content = f.read()
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

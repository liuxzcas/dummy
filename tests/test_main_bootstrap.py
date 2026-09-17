"""main.py 的模块加载顺序测试。

============ 被这个 bug 坑过(2026-09-17 实测) ============
现象:用户在 .env 里写 DUMMY_UI=line,但横分隔线/竖线一直不出现。

根因:**import 顺序错了**
    main.py:40   from ui import ui      ← import 时就读 DUMMY_UI 决定渲染器
    main.py:191  load_dotenv()          ← 到这里才加载 .env(太晚)
所以 .env 里的 DUMMY_UI 永远不会被 ui.py 看到。

实测复现:import ui 后是 SilentRenderer;load_dotenv() 再 reload 才变
LineRenderer。

修法:把 load_dotenv() 提到**所有项目模块 import 之前**。
这些测试锁住"顺序不能颠倒"这条不变量。
"""
import ast
import pathlib

MAIN = pathlib.Path(__file__).resolve().parent.parent / "main.py"


def _module_level_calls(tree):
    """返回模块顶层(不含函数/类内部)的语句列表。"""
    return [n for n in tree.body]


def test_main_exists():
    assert MAIN.exists(), f"找不到 {MAIN}"


def test_load_dotenv_called_at_module_level():
    """load_dotenv() 必须在**模块顶层**调用,不能在 main() 里。

    如果在函数里,import ui 时 .env 还没加载,DUMMY_UI/DUMMY_COLOR 全部失效。
    """
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    top_calls = [
        n for n in _module_level_calls(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
    ]
    names = []
    for c in top_calls:
        f = c.value.func
        names.append(getattr(f, "id", getattr(f, "attr", "")))
    assert "load_dotenv" in names, \
        "load_dotenv() 必须在模块顶层调用(见本文件头部的 bug 说明)"


def test_load_dotenv_precedes_project_imports():
    """**核心不变量**:load_dotenv() 必须在 import ui / core / llm 之前。

    这是本文件存在的原因 —— 顺序颠倒就会让 .env 里的配置静默失效,
    而且现象是"渲染样式不对",很难联想到"环境变量没加载"。
    """
    src = MAIN.read_text(encoding="utf-8")
    lines = src.split("\n")

    # 找模块顶层的 load_dotenv() 调用行（缩进为 0）
    load_line = None
    for i, line in enumerate(lines):
        if line.strip() == "load_dotenv()" and not line.startswith((" ", "\t")):
            load_line = i
            break
    assert load_line is not None, "模块顶层没有 load_dotenv()"

    # 找第一个项目模块 import 的行
    project_imports = ("from ui import", "from core import",
                       "from llm import", "from tools import",
                       "from colors import")
    first_import = None
    for i, line in enumerate(lines):
        if any(line.startswith(p) for p in project_imports):
            first_import = i
            break
    assert first_import is not None, "没找到项目模块 import"

    assert load_line < first_import, (
        f"load_dotenv() 在第 {load_line + 1} 行,但项目模块 import 在第 "
        f"{first_import + 1} 行 —— 顺序反了!\n"
        f"这会让 .env 里的 DUMMY_UI / DUMMY_COLOR 等变量静默失效。"
    )


def test_no_duplicate_load_dotenv_inside_main():
    """main() 里不该再有第二次 load_dotenv()(已提到模块顶层)。"""
    src = MAIN.read_text(encoding="utf-8")
    lines = src.split("\n")
    indented = [i + 1 for i, line in enumerate(lines)
                if line.strip() == "load_dotenv()" and line.startswith((" ", "\t"))]
    assert not indented, f"第 {indented} 行还有函数内的 load_dotenv() 调用,应删除"

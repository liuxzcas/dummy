"""readline 修复效果实测 —— 在真实终端里跑,对比输入行为。

============ 怎么用 ============
在**真实终端窗口**里跑(不能在管道/重定向下跑):

    python tests/manual/test_readline_effect.py

它会引导你做 4 个动作,每个动作后回答"是否正常"。
最后给出结论:你的环境修好了哪些、还有哪些问题。

为什么必须手动:input() 的行编辑行为只在真实 TTY 下才有意义,
管道/重定向测量不到(那时根本没有行编辑)。

============ 测什么 ============
  1. 中文退格       —— 输入"你好世界"后退 4 次,看是否干净
  2. 中英混排退格   —— 输入"ab你好cd"后退 2 次
  3. 方向键移动     —— 输入"abcdef"后按 ←,看光标是否移动
  4. 行内插入       —— 移动到中间后插入字符,看是否插在正确位置
  5. 历史翻页       —— ↑ 是否能看到刚才输入的内容
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from colors import paint, paint_prompt, GREEN, CYAN, YELLOW, GRAY  # noqa: E402


def show_env():
    """先报告环境(哪套 readline、是否启用了标记)。"""
    print("=" * 70)
    print("环境报告")
    print("=" * 70)

    print(f"  Python:   {sys.version.split()[0]}")
    print(f"  平台:     {sys.platform}")

    try:
        import readline
        rl_path = getattr(readline, "__file__", "(内置)")
        print(f"  readline: ✅ 可导入")
        print(f"             {rl_path}")

        # 判断是 GNU 还是 pyreadline3
        doc = (readline.__doc__ or "").lower()
        if "pyreadline" in doc:
            print(f"  后端:     ⚠️  pyreadline3(仿真实现,不处理 CJK 宽度)")
        elif "libedit" in doc or "readline" in doc and "pyreadline" not in doc:
            print(f"  后端:     GNU readline 或 libedit(通常是 GNU)")
        print(f"  文档串:   {readline.__doc__}")
    except ImportError:
        print(f"  readline: ❌ 无法导入(行编辑不可用)")

    # 提示符是否用 \001/\002
    os.environ["DUMMY_COLOR"] = "1"
    import importlib
    import colors as c
    importlib.reload(c)
    prompt = c.paint_prompt("你 > ", c.GREEN)
    has_mark = "\x01" in prompt
    mark_txt = "✅ 有 001/002 标记" if has_mark else "❌ 没有(无 readline 时不加,正常)"
    print(f"  提示符标记: {mark_txt}")

    print(f"  stdout 是 TTY: {sys.stdout.isatty()}")
    print()
    if not sys.stdout.isatty():
        print("  ⚠️⚠️  当前不是交互式终端 —— 行编辑不会生效!")
        print("       请在真实终端窗口里直接跑本脚本。")
        print()


def ask(question: str, detail: str = "") -> bool:
    """问用户一个是否题。"""
    print()
    print(f"  {question}")
    if detail:
        print(f"  {detail}")
    print()
    while True:
        ans = input("      结果正常吗? (y=正常 / n=有问题 / s=跳过): ").strip().lower()
        if ans in ("y", "n", "s"):
            return ans == "y"
        print("      请输入 y / n / s")


def main():
    show_env()

    from colors import paint_prompt, GREEN
    p = paint_prompt("你 > ", GREEN)

    results = []

    # ---- 1. 纯中文退格 ----
    print("=" * 70)
    print("测试 1/5:纯中文退格")
    print("=" * 70)
    print()
    print("  请操作:")
    print("    1) 输入: 你好世界")
    print("    2) 按退格键 4 次")
    print("    3) 观察: 是否每个字都被干净地擦掉?")
    print("       (不正常的现象:每按一次只擦掉半个字,留下残影)")
    print()
    input(f"  {p}")
    results.append(("纯中文退格", ask("4 次退格后,4 个字都干净消失了?")))

    # ---- 2. 中英混排 ----
    print()
    print("=" * 70)
    print("测试 2/5:中英混排退格")
    print("=" * 70)
    print()
    print("  请操作:")
    print("    1) 输入: ab你好cd")
    print("    2) 按退格键 2 次(应擦掉 'cd')")
    print("    3) 观察: 剩下的 'ab你好' 是否完整?")
    print()
    input(f"  {p}")
    results.append(("中英混排退格", ask("退格后 'ab你好' 完整、无残留?")))

    # ---- 3. 方向键 ----
    print()
    print("=" * 70)
    print("测试 3/5:方向键移动光标")
    print("=" * 70)
    print()
    print("  请操作:")
    print("    1) 输入: abcdef")
    print("    2) 按左方向键 3 次")
    print("    3) 观察: 光标是否向左移动了 3 个位置?")
    print("       (不正常的现象:出现字面量 ^[[D,光标不动)")
    print()
    input(f"  {p}")
    results.append(("方向键移动", ask("光标正常左移,没有出现 ^[[D?")))

    # ---- 4. 行内插入 ----
    print()
    print("=" * 70)
    print("测试 4/5:行内插入(验证光标位置真的对了)")
    print("=" * 70)
    print()
    print("  请操作:")
    print("    1) 输入: 甲乙丙丁")
    print("    2) 按左方向键 2 次")
    print("       —— 光标这时停在【丙】的前面:甲乙|丙丁")
    print("    3) 输入: X")
    print("    4) 期望结果: 甲乙X丙丁")
    print()
    print("  (说明:每按一次 ← 光标左移一【个字符】。")
    print("   甲乙丙丁 末尾起步,按 2 次后位于【丙】前面,所以 X 插在甲乙之后。)")
    print()
    input(f"  {p}")
    results.append(("行内插入", ask("结果是 '甲乙X丙丁'(光标位置正确)?")))

    # ---- 5. 历史 ----
    print()
    print("=" * 70)
    print("测试 5/5:↑ 翻命令历史")
    print("=" * 70)
    print()
    print("  请操作:")
    print("    1) 按 ↑ 方向键")
    print("    2) 观察: 是否出现上一次输入的内容?")
    print()
    input(f"  {p}")
    results.append(("↑ 翻历史", ask("↑ 能翻出上一条输入?")))

    # ---- 汇总 ----
    print()
    print("=" * 70)
    print("结论")
    print("=" * 70)
    print()
    ok = sum(1 for _, r in results if r is True)
    for name, r in results:
        mark = "✅" if r is True else ("⏭️" if r is None else "❌")
        print(f"  {mark} {name}")
    print()
    print(f"  {ok}/{len(results)} 项正常")
    print()
    print("  如果'方向键'或'历史'失败 → readline 没生效(检查 import)")
    print("  如果只有'中文退格'或'行内插入'失败 → readline 生效了,")
    print("     但后端不处理 CJK 宽度(Windows + pyreadline3 的已知局限)")
    print("=" * 70)


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# 诊断 dummy 的 Python 环境(依赖是否装齐、装在哪)
#
# 用法: bash check_env.sh
#
# 它回答:
#   1. 用哪个 Python(有几个?哪个可用?)
#   2. pip 的安装目标(SYSTEM / USER)
#   3. sys.path(解释器实际会搜的目录)
#   4. 5 个依赖能否 import
#   5. dotenv 装在哪
#   6. 按顺序该试什么
#
# ============ 为什么探测解释器而不是写死 python3 ============
# 写死 python3 在三种环境下会错:
#   · Windows:  python3 常命中 Microsoft Store 的占位符(报"Python was not found")
#   · 某些发行版:只有 python,没有 python3
#   · venv 内:应该用 venv 的 python,不是系统的
# 所以按"能用就选"的顺序探测(2026-09-18,与项目"不写死环境假设"的原则一致)。

set -u

# ---- 探测可用的 Python 解释器 ----
PY=""
for cand in python3 python python3.13 python3.12 python3.11 python3.10 py; do
    if command -v "$cand" >/dev/null 2>&1; then
        # 排除 Microsoft Store 的占位符(它会假存在但跑不了)
        if "$cand" -c "import sys" >/dev/null 2>&1; then
            PY="$cand"
            break
        fi
    fi
done

echo "=============================================================="
echo "1. Python 解释器"
echo "=============================================================="
if [ -z "$PY" ]; then
    echo "  ❌ 没有找到可用的 Python"
    echo
    echo "  Ubuntu 上安装:  sudo apt install python3 python3-pip python3-venv"
    exit 1
fi

echo "  选中的解释器: $PY"
echo "  路径        : $(command -v "$PY")"
echo "  版本        : $("$PY" --version 2>&1)"
echo
echo "  所有候选(前 5 个):"
for cand in python3 python python3.13 python3.12 python3.11 python3.10; do
    if command -v "$cand" >/dev/null 2>&1; then
        ok="可用"
        "$cand" -c "import sys" >/dev/null 2>&1 || ok="不可用(可能是 Store 占位符)"
        echo "    $cand → $(command -v "$cand")  [$ok]"
    fi
done 2>/dev/null | head -5

echo
echo "=============================================================="
echo "2. pip 的情况"
echo "=============================================================="
echo "  $PY -m pip 版本: $("$PY" -m pip --version 2>&1 | head -1)"
echo
echo "  USER_BASE(用户级安装根目录):"
"$PY" -c "import site; print('   ', site.USER_BASE)" 2>/dev/null || echo "    (取不到)"
echo "  user site-packages:"
"$PY" -c "import site; print('   ', site.getusersitepackages())" 2>/dev/null || echo "    (取不到)"
echo
echo "  是否在 venv 内:"
# 判据:venv 的 sys.prefix 与 sys.base_prefix 不同
# (系统 Python 两者相同,不能用 sys.prefix 是否存在来判断)
"$PY" -c "
import sys
if sys.prefix != sys.base_prefix:
    print('   是的 →', sys.prefix)
else:
    print('   否(用的是系统 Python:', sys.prefix, ')')
" 2>/dev/null || echo "    (取不到)"

echo
echo "=============================================================="
echo "3. sys.path(解释器实际会搜的目录)"
echo "=============================================================="
"$PY" -c "import sys; [print('   ', p) for p in sys.path]" 2>/dev/null

echo
echo "=============================================================="
echo "4. 逐个测试依赖能否 import"
echo "=============================================================="
missing=""
for mod in dotenv openai requests bs4 lxml json5 pytest; do
    if "$PY" -c "import $mod" 2>/dev/null; then
        ver=$("$PY" -c "
import $mod
print(getattr($mod, '__version__', getattr($mod, 'VERSION', '?')))
" 2>/dev/null)
        printf "  [OK]   %-10s %s\n" "$mod" "$ver"
    else
        printf "  [缺失] %-10s\n" "$mod"
        missing="$missing $mod"
    fi
done

echo
echo "=============================================================="
echo "5. dotenv 装在哪(如果装了)"
echo "=============================================================="
"$PY" -c "import dotenv; print('   模块位置:', dotenv.__file__)" 2>/dev/null \
    || echo "   (当前解释器看不到它)"

echo
echo "=============================================================="
echo "6. 结论与建议"
echo "=============================================================="
if [ -z "$missing" ]; then
    echo "  ✅ 依赖齐全 —— 可以运行: $PY main.py"
else
    echo "  ❌ 缺失:$missing"
    echo
    echo "  按顺序试:"
    echo "    a) $PY -m pip install -r requirements.txt"
    echo "       (用 '$PY -m pip' 而不是裸 pip,确保装到同一个解释器)"
    echo
    echo "    b) 若报 externally-managed-environment(Ubuntu 23.04+ 的 PEP 668 保护):"
    echo "       $PY -m venv .venv"
    echo "       source .venv/bin/activate"
    echo "       pip install -r requirements.txt"
    echo "       (推荐:依赖装在项目内 .venv/,符合'产物都在项目目录下')"
    echo
    echo "    c) 检查是不是装到了别的解释器:"
    echo "       $PY -c 'import sys; print(sys.executable)'"
fi
echo
echo "  注:warning 'script X is installed in ~/.local/bin which is not on PATH'"
echo "      只影响【命令行程序】(如 pytest 命令),不影响 Python 的 import。"
echo "      可以忽略 —— dummy 用的是 import,不是这些命令。"
echo "=============================================================="

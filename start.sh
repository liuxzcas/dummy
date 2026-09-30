#!/usr/bin/env bash
# Dummy Agent — Linux/macOS 启动脚本
#
# 用法:  bash start.sh      （或  ./start.sh  如果已 chmod +x）
#
# 它做三件事:
#   1. cd 到脚本所在目录 —— 无论从哪里调用,产物都落在项目内
#      (logs/ 和 session.db 都基于模块所在目录,见 docs/)
#   2. 探测可用的 Python —— 不写死 python3(某些环境只有 python)
#   3. 启动
#
# ============ 为什么不直接写 python3 main.py ============
# 写死 python3 在这些环境下会错:
#   · 有的发行版只有 python,没有 python3
#   · Windows 的 python3 常是 Microsoft Store 占位符(会提示去商店装)
#   · venv 激活后,python 指向 venv 的,系统路径可能仍是 python3
# 所以按"能用就选"的顺序探测 —— 与项目"不写死环境假设"的原则一致。

set -u

# ---- 1. 进入项目目录(脚本所在目录) ----
cd "$(dirname "$0")" || {
    echo "[错误] 无法进入脚本所在目录" >&2
    exit 1
}

# ---- 2. 探测 Python ----
PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import sys" >/dev/null 2>&1; then
        PY="$cand"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "[错误] 未找到可用的 Python" >&2
    echo "" >&2
    echo "  Ubuntu/Debian: sudo apt install python3 python3-pip python3-venv" >&2
    echo "  Fedora/RHEL:   sudo dnf install python3 python3-pip" >&2
    echo "  macOS:         brew install python3" >&2
    exit 1
fi

# ---- 3. 检查依赖(缺了就给安装提示,不直接崩) ----
if ! "$PY" -c "import dotenv" >/dev/null 2>&1; then
    echo "[提示] 缺少依赖,请先安装:" >&2
    echo "" >&2
    echo "    $PY -m pip install -r requirements.txt" >&2
    echo "" >&2
    echo "  若报 externally-managed-environment(Ubuntu 23.04+ 的保护)," >&2
    echo "  改用 venv(依赖会装在项目内 .venv/,不污染系统):" >&2
    echo "" >&2
    echo "    $PY -m venv .venv" >&2
    echo "    source .venv/bin/activate" >&2
    echo "    pip install -r requirements.txt" >&2
    echo "    ./start.sh" >&2
    echo "" >&2
    exit 1
fi

# ---- 4. 启动 ----
# terminal 工具需要 bash(本项目在 Linux 上直接用系统 bash,无需额外配置)
exec "$PY" main.py

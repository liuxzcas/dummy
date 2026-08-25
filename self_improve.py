"""
self_improve.py — 自我改进闭环(Phase 3 Step 4)

闭环:检测(dispatch 错误统计)→ 提案(LLM 根因分析,只读)
→ 用户批准 → 修改(受控区协议 §3.4 五道防线)
→ 验证门链 → 失败自动回退 + 教训 → 改进记录(§3.5 量化)。

安全设计(§3.4,强制前置):
- 三层分区:自由区(skills/docs/记忆/教训)/ 受控区(核心代码,
  修改必须用户批准)/ 禁止区(.git + 回退依赖,永不触碰)
- 五道防线:git 检查点 → 备份 .bak → 语法门 → 导入门 → 验证门+启动门
- 自动回退:验证失败 git checkout/备份还原 + 记教训 + 报告
- 写代码必须用户批准(D4-A);write_file 确认机制仍生效(双保险)

量化(§3.5):每次改进追加一行 JSONL 记录
(工具/文件/状态/验证结果/前后错误率快照)。
"""

import json
import os
import shutil
import subprocess
import sys

# 项目根(本文件上一级)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
# 改进记录(§3.5 量化评估数据源)
IMPROVE_LOG = os.path.join(PROJECT_ROOT, "logs", "self-improve.jsonl")

# 触发阈值(I4-A 定稿):错误数 >= MIN_ERRORS 且错误率 > ERROR_RATE_THRESHOLD
MIN_ERRORS = 3
ERROR_RATE_THRESHOLD = 0.30

# 禁止区:永不触碰(改坏它 = 救生艇沉没)
FORBIDDEN_DIRS = (".git",)

# 需要启动门验证的文件(core/main 改动必须证明 dummy 仍可启动)
BOOTSTRAP_FILES = ("core.py", "main.py")


def detect_tool_issue(stats: dict) -> str | None:
    """检测错误率超阈值的工具(I4-A);返回工具名或 None。

    条件(两个都要):错误数 >= 3(排除偶然)且错误率 > 30%(确认有病)。
    """
    for tool, s in stats.items():
        calls = s.get("calls", 0)
        errors = s.get("errors", 0)
        if calls >= MIN_ERRORS and errors / calls > ERROR_RATE_THRESHOLD:
            return tool
    return None


def _parse_proposal_response(content: str) -> dict:
    """解析提案 JSON(三级容错,与教训解析同款)。

    合法提案:{"root_cause": str, "changes": [{"file", "description"}],
    "verification": str}。changes 为空视为无提案。
    """
    text = (content or "").strip()
    if not text:
        return {}
    data = None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    if not data:
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
        if m:
            try:
                data = json.loads(m.group(1))
            except (json.JSONDecodeError, TypeError):
                pass
    if not isinstance(data, dict):
        return {}
    changes = []
    for ch in data.get("changes") or []:
        if isinstance(ch, dict) and ch.get("file"):
            changes.append({
                "file": str(ch["file"]).strip(),
                "description": str(ch.get("description") or "").strip(),
            })
    if not changes:
        return {}
    return {
        "root_cause": str(data.get("root_cause") or "").strip(),
        "changes": changes,
        "verification": str(data.get("verification") or "").strip(),
    }


def generate_proposal(llm, tool_name: str, stat: dict) -> dict:
    """LLM 根因分析 + 修复提案(只读,不修改任何文件)。

    输入:工具名、调用/错误统计、最近错误样本。
    输出:{"root_cause", "changes": [{file, description}], "verification"}。
    任何失败返回空 dict(不中断主流程)。
    """
    samples = "\n".join(f"- {s}" for s in (stat.get("error_samples") or [])) or "(无样本)"
    prompt = (
        "你是 Dummy Agent 的自我改进模块。分析工具的错误并给出修复提案。\n\n"
        f"工具: {tool_name}\n"
        f"统计: {stat.get('calls', 0)} 次调用, {stat.get('errors', 0)} 次错误\n"
        f"最近错误样本:\n{samples}\n\n"
        "任务(只读分析,不要修改任何文件):\n"
        "1. 根因分析:错误为什么会发生\n"
        "2. 修复提案:要改哪个文件(项目内路径,如 tools/terminal.py)、"
        "改什么(description 说明具体改动)\n"
        "3. 验证计划:如何验证修复有效\n\n"
        "输出 JSON(只输出 JSON):\n"
        '{"root_cause": "根因", '
        '"changes": [{"file": "相对项目根的文件路径", "description": "具体改动说明"}], '
        '"verification": "验证计划"}'
    )
    try:
        resp = llm.chat(messages=[{"role": "user", "content": prompt}])
        if isinstance(resp, dict):
            content = resp.get("content") or ""
        elif resp is not None and hasattr(resp, "get"):
            content = resp.get("content") or ""
        else:
            content = ""
    except Exception:
        return {}
    return _parse_proposal_response(content)


def _is_allowed_file(file_path: str) -> tuple[bool, str]:
    """文件可修改性检查(三层分区):禁止区拒绝,其余允许(批准已把关)。"""
    abs_path = os.path.abspath(os.path.join(PROJECT_ROOT, file_path))
    rel = os.path.relpath(abs_path, PROJECT_ROOT)
    if rel.startswith(".."):
        return False, f"路径在项目外: {file_path}"
    for part in rel.split(os.sep):
        if part in FORBIDDEN_DIRS:
            return False, f"禁止区文件,永不触碰: {file_path}"
    return True, ""


def _generate_new_content(llm, file_path: str, description: str) -> str:
    """LLM 基于原文件 + 修改说明生成修改后的完整内容。"""
    with open(file_path, encoding="utf-8") as f:
        original = f.read()
    prompt = (
        "你是 Dummy Agent 的自我改进模块。根据修改说明改写文件。\n\n"
        f"文件: {file_path}\n"
        f"修改说明: {description}\n\n"
        "要求:\n"
        "- 只做说明要求的改动,保持其余代码原样(不要重构、不要加功能)\n"
        "- 保持中文注释风格与缩进风格\n"
        "- 输出完整的新文件内容,不要截断,不要用省略号\n"
        "- 不要输出任何解释,直接输出文件内容"
    )
    resp = llm.chat(messages=[{"role": "user", "content": prompt}])
    if isinstance(resp, dict):
        content = resp.get("content") or ""
    elif resp is not None and hasattr(resp, "get"):
        content = resp.get("content") or ""
    else:
        content = ""
    # 围栏剥离
    if content.startswith("```"):
        lines = content.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines)
    return content


def verify_change(file_path: str, run_pytest: bool = True) -> list[str]:
    """验证门链(五道防线 ③④⑤⑥):语法门 → 导入门 → pytest → 启动门。

    返回失败消息列表(空 = 全过)。语法/导入失败提前返回(后门无意义)。
    项目小,pytest 全量 ~5 秒,用全量而非相关子集(S4 务实选择)。
    run_pytest=False 跳过 pytest 门(单测嵌套跑全量会超时,测试用)。
    """
    failures = []
    abs_path = os.path.abspath(os.path.join(PROJECT_ROOT, file_path))
    if not abs_path.endswith(".py"):
        return [f"仅支持 .py 文件验证: {file_path}"]
    # ③ 语法门
    r = subprocess.run(
        [sys.executable, "-m", "py_compile", abs_path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return [f"语法门失败: {(r.stderr or r.stdout)[:200]}"]
    # ④ 导入门(项目根 cwd)
    module = os.path.splitext(os.path.basename(abs_path))[0]
    r = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    if r.returncode != 0:
        return [f"导入门失败: {(r.stderr or r.stdout)[:200]}"]
    # ⑤ 验证门:pytest 全量
    if run_pytest:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q"],
            capture_output=True, text=True, cwd=PROJECT_ROOT,
        )
        if r.returncode != 0:
            tail = (r.stdout or r.stderr or "").strip().splitlines()
            return [f"pytest 门失败: {tail[-1] if tail else 'unknown'}"]
    # ⑥ 启动门:core/main 改动必须证明 dummy 仍可导入
    if os.path.basename(abs_path) in BOOTSTRAP_FILES:
        r = subprocess.run(
            [sys.executable, "-c", "from core import DummyAgent"],
            capture_output=True, text=True, cwd=PROJECT_ROOT,
        )
        if r.returncode != 0:
            return [f"启动门失败: {(r.stderr or r.stdout)[:200]}"]
    return failures


def rollback_file(file_path: str, backup_path: str | None) -> None:
    """回退:备份还原 + git checkout 双保险(救生艇)。"""
    abs_path = os.path.abspath(os.path.join(PROJECT_ROOT, file_path))
    if backup_path and os.path.isfile(backup_path):
        shutil.copy(backup_path, abs_path)
    subprocess.run(
        ["git", "checkout", "--", file_path],
        capture_output=True, cwd=PROJECT_ROOT,
    )


def append_improve_log(entry: dict) -> None:
    """改进记录(§3.5 量化评估:JSONL 追加)。"""
    os.makedirs(os.path.dirname(IMPROVE_LOG), exist_ok=True)
    with open(IMPROVE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

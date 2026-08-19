"""
skills_manager.py — 技能管理(Phase 3 Step 1)

技能 = skills/<name>/SKILL.md,采用 Anthropic SKILL.md 标准 + 工作流扩展:

- atomic(原子技能):只做一件事(检索/整理/成稿),跨流程复用
- workflow(工作流技能):编排原子技能,frontmatter 的 steps 引用清单

索引注入 system prompt(渐进式加载):
- 只注入名称 + 描述(一行一个,≤750 tokens 上限)
- 全文按需加载:LLM 用 read_file 读取 skills/<name>/SKILL.md
- 技能在 skills/ 自由区,git 管理,天然可回退(回退 = 删目录/git 还原)

设计决策(D5=B 原子技能+编排;安全:知识在数据层,不进代码)
"""

import os
import shutil

# 技能根目录(与 skills_manager.py 同级)
SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
# 索引注入上限:超出提示精简(Curator 后续处理,研究文档 1.1)
MAX_SKILLS = 15


def _parse_frontmatter(path: str) -> dict:
    """解析 SKILL.md 的 YAML frontmatter(name/description/type/steps)。

    容错:非 frontmatter 开头、无闭合、字段缺失都返回空 dict(跳过该技能)。
    """
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return {}
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 3)
    if end < 0:
        return {}
    meta: dict[str, str] = {}
    for line in content[3:end].splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip()
    return meta


def list_skills() -> list[dict]:
    """扫描 skills/ 目录,返回技能元信息(name/description/type)。

    按名称排序;frontmatter 解析失败的技能目录跳过。
    """
    result = []
    if not os.path.isdir(SKILLS_DIR):
        return result
    for name in sorted(os.listdir(SKILLS_DIR)):
        meta_path = os.path.join(SKILLS_DIR, name, "SKILL.md")
        if not os.path.isfile(meta_path):
            continue
        meta = _parse_frontmatter(meta_path)
        if meta and meta.get("name"):
            result.append({
                "name": name,
                "description": meta.get("description", ""),
                "type": meta.get("type", "atomic"),
            })
    return result


def load_skill(name: str) -> str | None:
    """读取技能 SKILL.md 全文;技能不存在返回 None。"""
    path = os.path.join(SKILLS_DIR, name, "SKILL.md")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def delete_skill(name: str) -> bool:
    """删除技能目录(自由区,git 可回退);不存在返回 False。"""
    path = os.path.join(SKILLS_DIR, name)
    if not os.path.isdir(path):
        return False
    shutil.rmtree(path)
    return True


def build_skills_index() -> str:
    """构建技能索引(注入 system prompt 的文本;无技能返回空串)。

    渐进式加载:索引只含名称+描述+类型标记,全文由 LLM 按需 read_file。
    """
    skills = list_skills()
    if not skills:
        return ""
    lines = [
        "## 可用技能(需要时用 read_file 读取 skills/<name>/SKILL.md 获取完整步骤)"
    ]
    for s in skills:
        wf = " [工作流]" if s["type"] == "workflow" else ""
        desc = s["description"][:100]
        lines.append(f"- {s['name']}{wf}: {desc}")
    if len(skills) > MAX_SKILLS:
        lines.append(
            f"(技能数 {len(skills)} 超上限 {MAX_SKILLS},建议精简)"
        )
    return "\n".join(lines)

"""
lessons.py — 教训生成(Phase 3 Step 3:错误学习)

错误学习:纠正/工具错误事件 → 反思生成教训规则 → 存入 lessons 表。
设计见 phase3-research.md §2(Reflexion/ExpeL 框架简化落地)、
§8(三库边界:教训 = 规则 do/don't,与记忆"事实"分库)。

生成方式:反思 prompt(事件 → 为什么错 → 一句话规则),三级容错
(与记忆抽取同款):异常 / 非法 JSON / 空结果 → 返回 []。
"""

import json
import re
from llm import message_content

# 用户纠正信号(强词,降低误报):命中即视为用户纠正,触发反思生成。
# 弱词(应该/不是/改一下)不触发——日常对话误报率高,教训由
# 强纠正与工具错误承载(Curator 后续可细化)。
CORRECTION_PATTERNS = re.compile(
    r"(错了|不对|搞错了|别这样|别用|别这么|不要|不应该|"
    r"重来|重新来|改成|换成|正确的是|正确做法|根本不是)"
)

# 工具**执行失败**的框架级标记(不含退出码——退出码要看数字,见 is_tool_error)。
# 保留此常量供外部引用/文档用途;判定逻辑已内联进 is_tool_error。
TOOL_ERROR_MARKERS = (
    "[ToolDispatch]",
    "[错误]",
)

# 单会话每轮工具错误反思上限(防连错连反思烧 token)
MAX_TOOL_ERROR_REFLECTIONS_PER_TURN = 1


def has_correction_signal(user_input: str) -> bool:
    """检测用户消息是否含纠正信号(强词)。"""
    return bool(CORRECTION_PATTERNS.search(user_input))


# 工具**未执行**的标记(用户取消 / 拒绝):既不是成功,也不是执行失败。
# 单独一类,因为它必须同时满足两个相反的要求:
#   - 对 is_tool_error 而言:要算"非成功"(否则"取消的命令"会被记成通过证据)
#   - 对"是否真执行过"而言:要能区分出来(不附退出码,不是真失败)
NOOP_MARKERS = (
    "[用户取消]",
    "[用户拒绝]",
    "[用户拒绝将内容写到项目目录之外]",
    "[未执行]",
)


def is_tool_error(result: str) -> bool:
    """判断工具返回是否为"执行失败"。

    ============ 判据(2026-09-16 修正) ============
    旧实现是三个字面量的子串匹配:
        ("[ToolDispatch]", "[错误]", "[EXIT CODE: ")
    它**两个方向都错**:
      - 误判成功:terminal 每条结果都附 "[EXIT CODE: 0]",而标记表里有
        "[EXIT CODE: " → **成功的命令被判成失败**(实测 2/2 成功样例全中招)。
        下游后果:每次成功命令都触发一次旁路 LLM"反思"(白烧 token)、
        被计入护栏 exact_failure(同命令同输出 6 次会被硬拦)。
      - 漏判失败:"[用户取消] 命令未执行"不含任何标记 → 被判成成功,
        "取消的 pytest"于是能当"通过证据"。

    新判据分三类:
      1. 未执行(用户取消/拒绝)→ **True**(不算成功)
         —— 为什么算 True:对消费方而言"这次调用没产出结果"与失败等价,
            都必须阻止它被当成"通过证据"。
      2. 有退出码标记 → **看数字**,非 0 才是失败
      3. 框架级错误标记 → True
    """
    text = result or ""
    if any(m in text for m in NOOP_MARKERS):
        return True
    if "[ToolDispatch]" in text or "[错误]" in text:
        return True
    # [EXIT CODE: N] 必须解析数字 —— 0 是成功,非 0 才是失败
    for m in re.finditer(r"\[EXIT CODE: (\d+)\]", text):
        if int(m.group(1)) != 0:
            return True
    return False


def generate_lesson(llm, event_text: str) -> list[dict]:
    """反思生成教训:事件 → 反思 → 规则条目 [{lesson, category}]。

    Reflexion 式"反思文本"的简化落地:一次 LLM 调用,输出一句话规则。
    三级容错:调用异常 / 非法 JSON / 空结果 → 返回 []。
    """
    prompt = (
        "你正在从一次交互中学习教训(错误学习)。\n\n"
        f"事件: {event_text}\n\n"
        "反思:发生了什么、为什么错、下次应该怎么做。\n"
        "如果这暴露了一条可复用的规则,输出 JSON 数组(最多 1 条):\n"
        '[{"lesson": "一句话规则,格式:做X会错,应该Y", "category": "类别"}]\n'
        "如果是信息不足或一次性失误,输出 []。\n"
        "只输出 JSON,不要其他内容。"
    )
    try:
        resp = llm.chat(
            messages=[{"role": "user", "content": prompt}],
        )
        content = message_content(resp)
    except Exception:
        return []
    return _parse_lessons_response(content)


def _parse_lessons_response(content: str) -> list[dict]:
    """解析教训 JSON(三级容错):标准 JSON → 围栏代码块 → 正则提取。

    合法条目:lesson 非空字符串。category 缺省 'general'。
    """
    candidates: list[dict] = []
    text = content.strip()
    if not text:
        return []
    # 第一级:标准 JSON 数组
    try:
        data = json.loads(text)
        if isinstance(data, list):
            candidates = data
    except (json.JSONDecodeError, TypeError):
        pass
    # 第二级:围栏代码块 ```json ... ```
    if not candidates:
        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
        if m:
            try:
                data = json.loads(m.group(1))
                if isinstance(data, list):
                    candidates = data
            except (json.JSONDecodeError, TypeError):
                pass
    # 第三级:正则提取 {lesson: "...", category: "..."} 片段
    if not candidates:
        m = re.search(r'"lesson"\s*:\s*"([^"]+)"', text)
        if m:
            cat = re.search(r'"category"\s*:\s*"([^"]+)"', text)
            candidates = [{
                "lesson": m.group(1),
                "category": cat.group(1) if cat else "general",
            }]
    result = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        lesson = str(item.get("lesson") or "").strip()
        if not lesson:
            continue
        result.append({
            "lesson": lesson[:200],
            "category": str(item.get("category") or "general")[:40],
        })
    return result

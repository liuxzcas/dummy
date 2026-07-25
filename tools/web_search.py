"""
tools/web_search.py — 网络搜索工具

使用 Tavily Search API 返回网络搜索结果。

=== 技术决策 ===

方案 A（当前）：Tavily Search API
  优点：专为 AI Agent 设计、返回结构化结果（标题/URL/内容/分数）、
        支持自动摘要（answer 字段）、稳定性高
  缺点：需要 API Key（免费注册 https://tavily.com，每月 1000 次免费查询）

方案 B：DuckDuckGo HTML 爬取（之前版本）
  优点：无需 API Key
  缺点：依赖页面结构、不稳定、易被限速、需要解析 HTML

方案 C：Google Custom Search API
  优点：结果质量最高
  缺点：配置复杂、免费额度低（每月 100 次）

选定方案 A，因为 Tavily 是专为 AI Agent 设计的搜索 API，
返回格式直接可用，不需要自己解析 HTML，结果质量稳定可靠。
结果中的 content 字段包含了页面摘要，比 DuckDuckGo 的 snippet 更完整。

=== API Key ===

通过环境变量 TAVILY_API_KEY 设置。如果不设置，工具会返回错误提示。
免费注册地址：https://tavily.com

=== 结果格式 ===

Tavily 返回的结构化结果直接格式化为 LLM 易读的文本：
  [1] 标题
      URL: https://...
      摘要内容...
  [2] 标题
      ...

Tavily 还支持自动生成 summary（answer），如果有则放在结果顶部。
"""

import os
import requests

TAVILY_URL = "https://api.tavily.com/search"


def web_search_handler(query: str, limit: int = 5) -> str:
    """使用 Tavily 搜索网络并返回结果列表。"""
    if not query.strip():
        return "[错误] 搜索查询不能为空"

    api_key = input("请输入你的 Tavily API Key: ").strip()
    if not api_key:
        return (
            "[错误] 未设置 TAVILY_API_KEY 。\n"
            "请在 https://tavily.com 注册获取免费 API Key，然后运行：\n"
        )

    try:
        resp = requests.post(
            TAVILY_URL,
            json={
                "api_key": api_key,
                "query": query,
                "max_results": limit,
                "search_depth": "basic",  # basic=快, advanced=深但慢
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

    except requests.exceptions.Timeout:
        return "[错误] 搜索请求超时（15 秒）"
    except requests.exceptions.ConnectionError:
        return "[错误] 无法连接搜索引擎（网络可能不可用）"
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        return f"[错误] Tavily API HTTP {status}: {e}"
    except requests.exceptions.RequestException as e:
        return f"[错误] 搜索失败: {type(e).__name__}: {e}"

    results = data.get("results", [])
    if not results:
        return f"未找到与 '{query}' 相关的结果"

    # Tavily 有时会返回一个自动生成的 summary（针对整个搜索主题）
    answer = data.get("answer", "")

    output = []
    if answer:
        output.append(f"📌 {answer}\n")

    output.append(f"搜索结果 ({len(results)} 条):")
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url = r.get("url", "")
        # Tavily 的 content 比 snippet 更完整，优先使用
        content = r.get("content", "") or r.get("snippet", "")
        output.append(f"\n[{i}] {title}")
        output.append(f"    URL: {url}")
        if content:
            # 截断过长内容（Tavily 有时返回很长的片段）
            if len(content) > 300:
                content = content[:297] + "..."
            output.append(f"    {content}")

    return "\n".join(output)

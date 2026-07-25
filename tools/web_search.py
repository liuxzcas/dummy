"""
tools/web_search.py — 网络搜索工具

使用 DuckDuckGo 搜索引擎返回网络搜索结果（无需 API Key）。

=== 技术决策 ===

方案 A（选定）：DuckDuckGo HTML 搜索（爬取公开搜索页面）
  优点：无需注册、免费、不需要 API Key
  缺点：依赖页面结构、请求频率受限（短时间大量搜索可能被临时封禁）

方案 B：Google Custom Search API
  优点：结果质量高、稳定
  缺点：需要 API Key + 有免费额度限制（每月 100 次）

方案 C：SerpAPI / Serper
  优点：专门为程序化搜索设计、稳定
  缺点：付费、需要 API Key

选定方案 A，因为 Phase 1 的目标是无外部依赖跑通工具调用。
后续升级到付费 API 只需要换 handler，注册表接口不变。

=== 结果格式 ===

每个搜索结果包含：
- title:      标题
- url:        链接
- snippet:    摘要文本

返回字符串格式（给 LLM 看的）：
  [1] 标题
      URL: https://...
      摘要内容...
  [2] 标题
      ...
"""

import json

import requests
from bs4 import BeautifulSoup


# DuckDuckGo 的 HTML 版搜索端点
# POST 请求，参数格式固定
DDG_URL = "https://html.duckduckgo.com/html/"

# 模拟正常浏览器的 User-Agent，部分站点会拦截非浏览器请求
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}


def web_search_handler(query: str, limit: int = 5) -> str:
    """搜索网络并返回结果列表。"""
    if not query.strip():
        return "[错误] 搜索查询不能为空"

    try:
        resp = requests.post(
            DDG_URL,
            data={"q": query},
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()

    except requests.exceptions.Timeout:
        return "[错误] 搜索请求超时（15 秒）"
    except requests.exceptions.ConnectionError:
        return "[错误] 无法连接搜索引擎（网络可能不可用）"
    except requests.exceptions.RequestException as e:
        return f"[错误] 搜索失败: {type(e).__name__}: {e}"

    # 解析搜索结果
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    # DuckDuckGo HTML 版的结果容器是 class="result" 的 div
    for i, result in enumerate(soup.select(".result")):
        if len(results) >= limit:
            break

        title_el = result.select_one(".result__title a")
        snippet_el = result.select_one(".result__snippet")
        url_el = result.select_one(".result__url")

        title = title_el.get_text(strip=True) if title_el else ""
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""

        # URL 可能在 href 或 data-nd 属性中
        url = ""
        if title_el and title_el.get("href"):
            url = title_el["href"]
        elif url_el and url_el.get("href"):
            url = url_el["href"]

        # DuckDuckGo 的链接是重定向链接，需要提取原始 URL
        # 格式通常为 //duckduckgo.com/l/?uddg=ENCODED_URL&...
        if "uddg=" in str(url):
            from urllib.parse import parse_qs, urlparse
            try:
                parsed = urlparse(str(url))
                params = parse_qs(parsed.query)
                if "uddg" in params:
                    url = params["uddg"][0]
            except Exception:
                pass  # 解析失败就保持原样

        if title:
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
            })

    if not results:
        return f"未找到与 '{query}' 相关的结果"

    # 格式化为 LLM 易读的文本
    output = [f"搜索结果 ({len(results)} 条):"]
    for i, r in enumerate(results, 1):
        output.append(f"\n[{i}] {r['title']}")
        output.append(f"    URL: {r['url']}")
        if r["snippet"]:
            output.append(f"    {r['snippet']}")
    return "\n".join(output)

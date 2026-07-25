"""
tools/web_extract.py — 网页内容提取工具

给定一个 URL，提取其中的可读文本内容（去除广告、导航栏等噪音）。

=== 技术决策 ===

方案 A（选定）：requests + BeautifulSoup 直接提取文本
  优点：轻量、零依赖配置、够用
  缺点：无法渲染 JavaScript（单页应用的内容提取不到）

方案 B：Selenium / Playwright（无头浏览器）
  优点：能渲染 JS、能处理 SPA
  缺点：需要安装浏览器驱动、内存消耗大、速度慢
  理由（不选）：Phase 1 不需要处理 SPA，JS 渲染在之后的阶段再加

方案 C：Trafilatura（专用文本提取库）
  优点：专门为从 HTML 提取正文设计、准确率高
  缺点：多一层依赖、对复杂页面的容错不如 BS4 + 手动规则灵活
  理由（不选）：Phase 1 手动控制提取逻辑，便于后续调优

=== 提取策略 ===

1. 先用 charset 检测或 meta 标签确定编码
2. 解析 HTML，移除 script、style、nav、footer 等噪音元素
3. 尝试提取 article 标签或 body 中的文本
4. 按 char_limit 截断，尾部注明剩余内容

=== 与 read_file 的关系 ===

read_file 用于读取本地文件（支持 offset/limit 翻页）。
web_extract 用于读取在线网页（一次性返回，不支持翻页——因为网页
内容不按行组织，分页没有意义）。
"""

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}


def web_extract_handler(url: str, char_limit: int = 10000) -> str:
    """提取网页的可读文本内容。"""
    if not url.strip():
        return "[错误] URL 不能为空"

    # 自动补全协议（LLM 经常只给域名不加 https://）
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        return f"[错误] 请求超时（20 秒）: {url}"
    except requests.exceptions.ConnectionError:
        return f"[错误] 无法连接: {url}"
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        return f"[错误] HTTP {status}: {url}"
    except requests.exceptions.TooManyRedirects:
        return f"[错误] 重定向次数过多: {url}"
    except requests.exceptions.RequestException as e:
        return f"[错误] 请求失败: {type(e).__name__}: {e}"

    # 检测编码（优先用 Content-Type header，次选 meta charset）
    # requests 会尽量自动检测，这里做一层兜底
    if resp.encoding and resp.encoding.lower() == "iso-8859-1":
        # 部分服务器对非拉丁字符集返回 iso-8859-1 作为默认值
        # 尝试从 meta 标签或内容中检测真实编码
        resp.encoding = resp.apparent_encoding

    try:
        soup = BeautifulSoup(resp.content, "html.parser", from_encoding=resp.encoding)
    except Exception as e:
        return f"[错误] 页面解析失败: {e}"

    # ---- 提取策略 ----
    # 1. 移除不需要的元素
    for tag in soup(["script", "style", "nav", "footer", "header",
                     "aside", "noscript", "iframe", "form"]):
        tag.decompose()

    # 2. 尝试从 article 标签提取（效果最好）
    article = soup.find("article")
    if article:
        text = article.get_text(separator="\n", strip=True)
    else:
        # 3. 没有 article 标签，从 body 提取
        body = soup.find("body")
        if body:
            text = body.get_text(separator="\n", strip=True)
        else:
            text = soup.get_text(separator="\n", strip=True)

    # 4. 清理空行
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    text = "\n".join(lines)

    if not text:
        return f"[警告] 未能从页面提取到内容: {url}"

    # 5. 按 char_limit 截断
    title = soup.title.string.strip() if soup.title and soup.title.string else url
    header = f"标题: {title}\n来源: {url}\n{'─' * 40}\n"

    if len(text) <= char_limit:
        return header + text
    else:
        truncated = text[:char_limit]
        remained = len(text) - char_limit
        return (header + truncated
                + f"\n\n[... 内容截断，剩余约 {remained} 字符未显示。"
                  f"如需提取更多内容，请缩小提取范围或指定具体段落 ...]")

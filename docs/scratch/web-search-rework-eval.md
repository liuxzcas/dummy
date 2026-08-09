# 设计讨论:web_search 改造为"直接操作浏览器搜索"的评估(2026-08-10)

> 状态:讨论记录(scratch,未定稿)
> 问题:把现有基于 Tavily 的 web_search.py 改为直接操作浏览器打开搜索引擎
> 搜索并解析结果,要求效果不落后于 Tavily,工作量多大?

## 现状要点

- `tools/web_search.py`:POST api.tavily.com/search,basic 深度,15s 超时
- 返回:可选 answer(整题自动摘要)+ results[](title/url/content/snippet),
  其中 content 是**页面级摘要**(不只是 SERP snippet)
- 输出格式:[i] 标题 / URL / 内容(截断 300)
- **已知问题:API key 用 `input()` 交互式输入**(docstring 说环境变量但代码
  不是)——与 terminal 工具确认框同类问题,非交互环境会 EOF,需改环境变量读取

## 三档工作量(单人,熟悉 Python)

| 档位 | 方案 | 工作量 | 效果对比 | 主要风险 |
|------|------|--------|---------|---------|
| 1 | requests 直抓 DDG HTML / Bing + BeautifulSoup 解析 | 1-2 天 | snippet 级≈持平,中文搜索略差 | 同 IP 高频查询易限速,结构易变 |
| 2 | Playwright 无头 + Bing 主 + DDG 回退 + 选择器容错 + 限速重试 | 约 1 周 | 基本持平(snippet 级) | 反爬随时间升级,需持续维护 |
| 3 | Playwright 真实浏览器 + 指纹/代理池 + 3 引擎轮换 + 可选 content 抓取 | 2-3 周 | 完全持平甚至更全 | 维护税每月 2-4 小时,复杂度高 |

## "效果不落后"的难点(Tavily 的价值拆解)

1. **标题/URL/snippet**——最容易:引擎原生结果质量不差,解析 SERP 即可,
   1-2 天可做到 90%
2. **content 页面摘要**——最难:Tavily 的 content 是抓取页面正文后的摘要,
   不是 SERP snippet(150-200 字符)。要对齐需每查询再抓 5 个结果页
   (web_extract):延迟 1s → 10-30s,反爬面扩大 5 倍。**要不要这一项,
   决定停在档位 1/2 还是上档位 3**
3. **answer 自动摘要**——自建需用 LLM 补或去掉
4. **稳定性**——Tavily 的隐藏价值是替你付了反爬维护税;搜索引擎改版
   (Google 每季度调 SERP 结构)+ 反爬升级是持续成本,不是一次性

## 建议(按动机)

- **摆脱 Tavily 依赖/费用**:最优解不是操作浏览器,而是换免费搜索 API
  (Brave 免费 2000 次/月、Serper 2500 次/月)或 ddgs 库——1-2 小时,
  效果持平;浏览器方案是杀鸡用牛刀
- **教学目的**(理解浏览器自动化/SERP 解析/反爬对抗):按档位 2(约 1 周),
  放 Phase 2.3b 之后。档位 2 已覆盖全部核心知识点,档位 3 的指纹/代理是
  工程化脏活,教学收益边际递减
- **不建议直接上档位 3**:对 dummy-agent 的偶发搜索频率和教学定位,维护税不划算

## 附带待办

- web_search.py 的 `input()` 读 key 改为环境变量读取(TAVILY_API_KEY),
  否则非交互环境 EOF(与 terminal 确认框同坑)

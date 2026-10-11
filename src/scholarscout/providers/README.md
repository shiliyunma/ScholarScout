# providers/ —— 检索来源与模型客户端

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的四问组织，只写**不随代码变的那部分**：这一层是干什么的、先看哪个文件、哪些是 API、去哪看更多。
> **文件清单、行数、导出符号、依赖方向刻意不写在这里** —— 由 `scripts/make_code_map.py` 生成到 `docs/代码地图/00-符号清单.md`。

## 1. 这个目录放什么

两类"外部提供方"，都只负责**取数据**，不落库（规则：`providers/*` 不许 import `state/*`，落库是 `tools/` 的事）。

- **检索来源**：每个来源一个文件，实现同一个 `base.SearchProvider` 协议，把各家五花八门的返回压成统一的 `base.SourceCard`。学术（`arxiv` / `openalex` / `semantic_scholar`）、代码（`github`）、通用与商用（`bing` / `duckduckgo` / `searxng` / `tavily` / `serper` / `brave` / `google_cse`）。
- **模型客户端**：`llm.py`（OpenAI 兼容 HTTP 接口，DeepSeek 与本地 Ollama 同一套代码）。
- `base.py` 放公共类型与 URL 规范化；`websearch.py` 放通用网页来源的共用件；`settings.py` 放来源开关、`auto` 解析与限速参数。
- **出网一律经 `fetch/http.py`**（各来源不直接 `import httpx`）。**模型调用是例外**：它不属于抓取层，`llm.py` 自己带 `httpx` 客户端（见其模块说明）。

## 2. 先看哪个文件

1. **`base.py`** —— 先读它：`Query` / `SourceCard` / `SearchProvider` 与 `canonical_url` 是所有来源的公共语言。
2. **`settings.py`** —— 来源清单（`KNOWN` / `IMPLEMENTED` / `KEYLESS` / `COMMERCIAL_KEYS` / `AUTO_ORDER`）、`auto` 解析、`provider_statuses`（界面"哪些能用、为什么不能用、怎么才能用"就靠它）。
3. **`arxiv.py`** / **`github.py`** —— 两个最完整的来源，看它们怎么写 `search()` 与离线可测的解析函数。
4. **`websearch.py`** —— 网页来源的共用件（跳转壳解开、非内容结果挡掉）。
5. **`llm.py`** —— 模型客户端（错误分类 `LlmError`、`ChatReply.usage` / `truncated`）。
6. `__init__.py` —— 包标记。

## 3. 哪些是 API（外部可依赖的契约）

调用方在 `tools/research_search.py`（检索）、`runtime/subtasks.py`（并行扇出）、`runtime/loop.py` / `runtime/toolctx.py`（模型客户端）、`summary/pipeline.py`（摘要用 `llm`）、`rpc/`（来源状态）。改签名 = 破坏契约。

- `base.Query` / `base.SourceCard`（`.canonical` / `.domain` / `.to_row()`）/ `base.SearchProvider` / `base.canonical_url` / `base.dedupe_cards` / `base.year_from_iso`
- 各来源的 `*Provider` 类：`ArxivProvider` / `OpenAlexProvider` / `GithubProvider` / `BingProvider` / `DuckDuckGoProvider` / `SearxngProvider` / `TavilyProvider` / `SerperProvider` / `BraveProvider` / `GoogleCseProvider` / `SemanticScholarProvider`，统一 `name` + `min_interval_s` + `search(query)`
- 各来源的解析纯函数（离线可测）：`arxiv.parse_feed` / `arxiv.build_url` / `arxiv.build_query_expression`、`openalex.parse_results` / `openalex.abstract_from_inverted_index`、`github.parse_results` / `github.card_from_repo`、`semantic_scholar.parse_results`、`websearch.card_from` / `websearch.cards_from_pairs` / `websearch.unwrap_redirect` / `websearch.is_content_url`
- `settings.SearchSettings` / `settings.ProviderStatus` / `settings.provider_statuses` / `settings.api_key_for` / `settings.optional_token`
- `llm.LlmClient`（协议）/ `llm.OpenAIChatClient` / `llm.create_client` / `llm.ChatReply` / `llm.ToolCall` / `llm.LlmError`

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- 设计依据与口径（**权威副本，改口径先改它们**）：
  - `docs/03-目录结构与规则文件.md` §2（`providers` 不许 import `state`）、§5（`providers/*` 属关键路径，看限速与密钥使用）、§6（通用检索为什么没有"一键免 key"）、§7（坑清单）
  - `docs/01-接口契约.md` §5.1（`research.search` 的三层来源）
- 参数：`config.example.yaml` 的 `search:` 段（来源开关、`min_interval_s`、`searxng_base_url`、`openalex_mailto`、`allow_searchpage_scraping`）
- 守这一层的测试：`tests/providers/`（`test_arxiv` / `test_github` / `test_base` / `test_academic_indexes` / `test_web_providers` / `test_provider_status` / `test_llm_parse` / `test_llm_cache` / `test_llm_thinking`）
- 离线样例：`tests/fixtures/`（如 `arxiv_sample.xml`）；另有 `tests/tools/test_search_dedup.py`、`tests/fetch/test_search_cache.py`
- 通用检索自救与自检：`scripts/searxng_up.py`、`scripts/check_search_sources.py`；SearxNG 部署在 `deploy/searxng/`

## 改这个目录之前先知道

- **arXiv 三个源码级事实**：必须显式带 `Accept`（不带一律 406）；间隔用到 ≥3 秒/次（连续请求会被限速）；**它只索引英文** —— 中文关键词检索必然 0 条，是事实不是缺陷（检索用英文关键词，中文只留给主题与摘要）。见 `docs/03 §7`。
- **查询不要整句加引号**：`all:"a b c"` 会被当成精确短语、几乎恒 0 条；`arxiv.build_query_expression` 的做法是**按空格切词、各自 `all:` 并用 AND 组合**。
- **robots 对官方 API 有豁免白名单**：`export.arxiv.org` 在 `fetch/http.py::DEFAULT_ROBOTS_EXEMPT_HOSTS` 里 —— arXiv 的 robots 禁 `/api/query`，照字面执行会让检索整体不可用。**网页结果页**（`cn.bing.com` 等）默认不抓，要抓得用户显式打开 `allow_searchpage_scraping`。
- **检索结果不进 HTTP 缓存**：检索是**时点查询**，各来源调 `http.fetch(..., cache=False)`；只有**内容抓取**才吃缓存（`docs/03 §7`）。
- **`github` 的限速不写进默认表**：它的最小间隔取决于有没有配 `GITHUB_TOKEN`（未认证 Search API 10 次/分钟 → 6s；认证 → 2s），由 `providers/github.py` 自己算。写进 `min_interval_s` 表会**覆盖**它、把额度打光（表现为间歇 403）—— 所以 `settings.DEFAULT_MIN_INTERVAL` 故意不含 `github`，`interval_for` 的 fallback 走 provider。
- **`auto` 只带"现在真能用"的来源**：`settings.SearchSettings.auto_names` 与 `settings.provider_statuses` 的判定口径必须一致 —— 不一致会出现"界面说可用、检索时却不带它"。抓取类来源（`bing` / `duckduckgo`）不进 `auto`（一个 robots 禁、一个国内需梯子，带上就每次撞墙）。
- **`provider_status` 的错误码要分三类**：`missing_api_key: <ENV>` / `missing_config: <配置键>` / `blocked_by_robots`（`docs/03 §7`）—— 只说"缺 key"用户不知道要配哪个。

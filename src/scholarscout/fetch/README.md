# fetch/ —— 出网与正文抽取层

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的四问组织，只写**不随代码变的那部分**：这一层是干什么的、先看哪个文件、哪些是 API、去哪看更多。
> **文件清单、行数、导出符号、依赖方向刻意不写在这里** —— 由 `scripts/make_code_map.py` 生成到 `docs/代码地图/00-符号清单.md`。

## 1. 这个目录放什么

两个职责：**(a) 全项目唯一的出网口**，**(b) 把抓到的字节转成正文**。

- **出网只此一处**（`docs/03 §2`）：限速（按 host 串行化）、指数退避重试、缓存、UA、超时、体积上限、robots 全集中在 `http.py`。**重试也只写在这个文件**（规则 §3.5）。其他模块不许 `import httpx`（`tests/test_layering.py` 拦）。
- **正文抽取**：HTML → `html_text.py`；Office 三件套 `.docx/.xlsx/.pptx` → `office.py`；电子书 `.epub` → `formats.epub_text`；压缩包 `.zip/.tar*` → `formats.archive_listing`（**只列目录，不解内容**）；RTF → `formats.rtf_text`。**PDF 抽文字的主入口不在这里** —— 在 `tools/source_fetch.py`（本地 `pymupdf`）；本目录的 `mineru.py` 只是抽不出文字层时的**兜底**。
- **本层是最底层**：只依赖标准库（`http.py` 用已批准的 `httpx`）。`tools/`、`rpc/` 可以依赖它，它不许反向依赖上层。
- `feishu_send.py` / `feishu_onboard.py` 也落在这里：它们是飞书**出网**能力，因为"出网只走 `fetch/`"才放这层；它们不直接 `import httpx`，都经 `HttpClient`。渠道层（`channels/`）不许 import `fetch`，所以经**入口层**注入使用。

## 2. 先看哪个文件

1. **`http.py`** —— 出网唯一入口，也是**关键路径**（`docs/03 §5`，人工逐行 review）。先读它。
2. **`html_text.py`** —— HTML → 正文（stdlib `HTMLParser`，主内容优先）。
3. **`office.py` / `formats.py`** —— 各类文件的正文抽取器（抽不到就如实返回 `ok=False` + 原因，不返回空串冒充成功）。
4. **`mineru.py`** —— 扫描版/复杂版式 PDF 的兜底（官方 API，契约与实测写在文件顶部）。
5. **`settings.py`** —— 抓取参数（与 `config.example.yaml` 的 `fetch:` 段对应）。
6. **`feishu_send.py` / `feishu_onboard.py`** —— 只有做飞书渠道时才看。
7. `__init__.py` —— 纯包标记，无逻辑。

## 3. 哪些是 API（外部可依赖的契约）

调用方主要在 `tools/source_fetch.py`、`rpc/archive.py`（按需下载）与各 `providers/*`（都经 `HttpClient` 出网）。改签名 = 破坏契约。

- `http.HttpClient`（`fetch` / `post_json` / `post_form` / `upload_bytes` / `robots_allowed` / `close`）、`http.FetchError`（带 `kind`，由 `rpc/errors.py` 映射成契约错误码）、`http.FetchResult`（`.text` / `.json()` / `.charset`）、`http.DEFAULT_ROBOTS_EXEMPT_HOSTS`
- `html_text.extract_html(html) -> HtmlDocument`（`.title` / `.text` / `.lang` / `.used_main`）
- `office.office_text(extension, content)`、`office.docx_text` / `office.xlsx_text` / `office.pptx_text`
- `formats.guess_extension(media_type, url, is_pdf)`、`formats.epub_text` / `formats.rtf_text` / `formats.archive_listing` / `formats.is_archive`，以及后缀集合常量 `OFFICE_SUFFIXES` / `MEDIA_SUFFIXES` / `TEXT_SUFFIXES` 等
- `mineru.parse_pdf_bytes` / `mineru.parse_pdf_url` -> `MineruText`（`.ok` / `.state` / `.engine` / `.detail`）
- `settings.mineru_token(env)`、`settings.FetchSettings`
- `feishu_send.build_sender` / `feishu_send.send_text` / `feishu_send.text_content`、`feishu_onboard.fetch_tenant_token` / `feishu_onboard.probe_bot`（渠道层经入口层注入，别处不要 import）

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- 设计依据与口径（**权威副本，改口径先改它们**）：
  - `docs/03-目录结构与规则文件.md` §2（分层与"出网唯一入口"）、§5（关键路径）、§7（真机坑清单）
  - `docs/04-摘要提取方案.md` §7（MinerU 接入：两条接口与实测）
  - `docs/19-资料命名与放置方案.md` §8.3（大文件"抽取阈值"与 `extract_status`）
- 参数：`config.example.yaml` 的 `fetch:` 段
- 守这一层的测试：`tests/fetch/`（`test_http` / `test_html_text` / `test_office` / `test_mineru` / `test_mineru_upload` / `test_search_cache` / `test_feishu_send` / `test_feishu_onboard`）、`tests/test_layering.py`（分层）、`tests/tools/test_source_fetch.py`、`tests/tools/test_mineru_fallback_order.py`
- 离线样例：`tests/fixtures/`
- 相关脚本：`scripts/check_mineru.py`（扫描版自检）、`scripts/check_search_sources.py`（真机来源自检）

## 改这个目录之前先知道

- **arXiv 的 `Accept` 头**：不带会一律 406。`http.DEFAULT_ACCEPT` 默认就带；`providers/arxiv.py` 还会再显式声明一次自己的 `Accept`。
- **robots 只豁免官方 API 主机**：`DEFAULT_ROBOTS_EXEMPT_HOSTS` 默认含 `export.arxiv.org` —— arXiv 的 robots 明确禁 `/api/query`，照字面执行会让**官方检索 API 整体不可用**（`docs/03 §7`）。要抓**网页结果页**（如 `cn.bing.com`）必须由用户显式打开开关（`providers/settings.py::SearchSettings.allow_searchpage_scraping`），不是本层默认行为。
- **缓存只缓存"成功且有内容"的响应**：4xx/5xx 与空响应**不写缓存**（写进去等于把一次瞬时故障钉死，`docs/03 §7`）。**检索类请求一律 `cache=False`**（检索是时点查询），只有内容抓取才吃缓存。
- **`upload_bytes` 刻意不带 `Content-Type`**：MinerU 的预签名地址把它算进了签名，带上就 `403 SignatureDoesNotMatch`；预签名地址也**不走缓存、不过 robots**。
- **"优先抓真文件、不抓落地页"由调用方决定**：本层只按给到的 URL 出网。`tools/source_fetch.py` 会把 arXiv 的 `/abs/<id>` 换成 `/pdf/<id>`，并在返回里如实写三个字段 —— `requested_url`（用户要的）、`fetched_url`（真正抓到的）、`used_landing_page`（`fetched_url == url`，即是否仍在抓落地页）。
- **抽取失败要能区分"没内容"与"打不开"**：抽取器一律 `(ok, text, detail)` 三返回，`ok=False` 带原因，不返回空串冒充成功（`office.py` / `formats.py` 的口径）；MinerU 失败记 `mineru_failed` + 原因，**不阻塞**整条流水线。

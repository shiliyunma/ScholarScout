---
name: rag-project
description: >-
  RAGLearn 本地知识库项目 — E:\RAGLearn 的完整上下文。
  加载此 skill 即可了解项目全貌，无需扫盘。
---

## 项目概览

基于本地文档构建的 RAG 问答系统，使用 ChromaDB 向量库 + Cross-Encoder 重排序 + LLM 生成回答。提供 Streamlit Web UI 和 CLI 两种交互，查询自动记录 MySQL 日志。

**项目根路径:** `E:\RAGLearn`

## 核心架构

```text
用户 → 查询改写(LLM生成2-3个同义查询)
  → Embedding(bge-large-zh, GPU) → ChromaDB多查询检索+去重合并
  → Reranker(bge-reranker-base, GPU) 全部评分（全排序）
  → parent_merge 从高分到低分遍历，取前 5 个不同 parent
  → 替换 child.text 为 parent_text → 两阶段流式输出
  → MySQL日志 (ragsearch.qa_logs, 含 turn_number)
  ← 多轮对话：下一问携带历史，可选检索或纯 LLM

习题模式（并行分支）：
  用户查询含"习题/题目/练习"等关键词
  → 提取关键词 → MySQL exercise_index 表 BM25+LIKE 搜索
  → 展示：题干 + 答案 + 解析摘要
  → 不走 ChromaDB/Embedding/Reranker 流水线
```

**关键流程细节：**
- Reranker 评**全部**候选（非仅 Top-5），parent_merge 从全排序队列中取前 5 个不同 parent。如果 Top-5 中有多 child 属同一 parent，自动从后续候选补充，保证始终展示 5 条（除非全队列不足 5 个不同 parent）。
- 改写信息只展示与用户原文不同的查询（去重），通过 `multi_retrieve()` 的第 4 返回值 `display_queries` 透传。
- 改写用的 LLM 与回答用的 LLM 相同（DeepSeek API），改写风格为同义扩展。

## 文档类型（恢复 exercise 后）

原始文档经语义预切分为三种 type：

| type | 含义 | 知识库模式 | 习题模式 |
|------|------|-----------|---------|
| content | 正文知识点 | √ | — |
| exercise | 练习题题干 | — | √ |
| solution | 答案与解析 | — | √ |

注：exercise 曾在 D11 被移除，2026-07-07 的 D29 恢复存储。知识库模式只检 `type=content`，习题模式检 `type=exercise+solution`。

## 入口文件

| 文件 | 用途 |
|------|------|
| `launch.py` | **推荐入口** — 纯净启动 Web UI，压制全部警告和日志 |
| `app.py` | Streamlit Web UI（左右分栏布局：左侧历史列表 + 右侧搜索/结果）；UI 风格见 `references/ui-style-guide.md` |
| `query.py` | CLI 问答，支持 `--raw` `--rerank-only` `--no-rewrite`（关闭查询改写）；`multi_retrieve()` 返回 `(docs, embed_ms, retrieval_ms, display_queries)` 四元组 |
| `ingest.py` | 文档导入，支持 `--reset` 清空重建；含 parent-child 双层切分 + **章节元数据解析**（subject/chapter/chapter_title/section/section_title） |
| `config.py` | 集中配置，含 `REWRITE_ENABLED`、`PARENT_CHUNK_SIZE` 等调优参数 |
| `scripts/clean_王道.py` | MinerU 导出的王道考研教材清洗：删封面/广告/图片占位/OCR 整饰 → 输出到 `clean_docs/王道考研/` |
| `scripts/build_exercise_index.py` | 解析 4 本教材的习题↔解析映射，写入 MySQL `exercise_index` 表（~2400 条） |

## 模型

| 模型 | 用途 | 设备 | 路径 |
|------|------|------|------|
| bge-large-zh-v1.5 | 嵌入 (1024d) | GPU CUDA | models/bge-large-zh-v1.5 |
| bge-reranker-base | 重排序 | GPU CUDA | models/bge-reranker-base |
| bge-small-zh-v1.5 | 轻量嵌入备选 (384d) | — | models/bge-small-zh-v1.5 |

从 hf-mirror.com 下载，中国大陆网络友好。

## 分块架构 (Parent-Child)

项目使用 parent-child 双层切分，检索用 child（512字符，精确定位），送 LLM 用 parent（~2000字符，上下文完整）。采用**偏移映射方案**消除 tail loss。

### 层级关系

```
原始文档 → 语义预切分(content/solution)
    │
    ├─[1] 连续滑动窗口 → child ~4300块 → ChromaDB(向量检索)
    │                                          metadata含 parent_id + parent_text
    │
    └─[2] 语义边界切分 → parent ~1700块（带字符偏移范围）
                 ↓
        [3] 偏移量映射：每个 child 的起始偏移 → 匹配所属 parent
            ── 两阶段独立运算，无 parent 边界打断 child 滑动窗口 ──

查询时:
  ChromaDB检索 Top-20 → Reranker 全部评分（全排序）
  → parent_merge() 从高分到低分遍历，取前 5 个不同 parent
  → child.text 替换为 parent_text → 送 LLM
  注：Reranker 不再截断到 5 条，而是返回全部评分结果供 parent_merge 兜底补充。
  如果 Top-5 中有多个 child 属于同一 parent，parent_merge 自动从后续候选者中填补，
  确保始终展示 5 条（除非全队列中不同 parent 不足 5 个）。
```

### 各层参数

| 层级 | 大小 | overlap | 用途 |
|------|:----:|:-------:|------|
| parent | 2000字符 | 0（无重叠） | 送 LLM 的完整语义段落；用 RecursiveCharacterTextSplitter(\n\n→\n→。) 切分 |
| child | 512字符 | 128字符 | 向量检索的最小单元；连续滑动窗口，不受 parent 边界打断 |

### 与旧版（per-parent 切 child）的区别

| 维度 | 旧版（parent 内切 child） | 新版（偏移映射） |
|------|:-:|:-:|
| child 切分范围 | 在单个 parent text 上切 | 在整段 text 上连续滑动 |
| 跨 parent 边界的 child | ❌ 不存在 | ✅ 有（通过偏移量归属） |
| 总块数 | ~7,600 | **~4,269** |
| tail loss（不满512的废块） | 每个 parent 末尾有一个 | ❌ 无 |
| 边界不精确度 | — | <1%（起始偏移落入隔壁 parent 范围） |
| ingest 稳定性 | 超时（7600块×119批） | ✅ 稳定（4269块×67批） |

## LLM

- **默认:** DeepSeek API — `https://api.deepseek.com/v1`，模型 `deepseek-v4-flash`
- **备选:** Ollama 本地 Qwen2.5-3B — `ollama pull modelscope.cn/qwen/Qwen2.5-3B-Instruct-GGUF`
- config.py 的 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 改即可切换
- **注意:** 所有 LLM 调用使用 `requests.post()` 直调，不用 openai 客户端（httpx 在 Windows+git-bash 连 localhost 报 Win10061）

## 部署要点

1. **启动:** `python launch.py` → `http://localhost:8501`
2. **重启前必杀旧进程:**
   - `netstat -ano | grep ':8501' | grep LISTENING` 查出所有 PID
   - `taskkill //F //PID <pid>` 逐个杀
3. **Streamlit 绑定 0.0.0.0**，IP 可直接访问，不必加 `--server.address`
4. **CUDA torch 安装慢**（中国网络）→ 从系统 Python site-packages 复制 torch* 到 venv

## UI 布局（DeepSeek 风格）

### 整体结构（多轮对话模式）

```text
┌──────────┬──────────────────────────────────────┐
│ 左侧栏    │  右侧聊天区                           │
│ (280px)  │                                      │
│          │  ┌── [用户消息] ─────────────┐        │
│ [Logo]   │  │  什么是B+树？               │        │
│ 🔍  ◀   │  └──────────────────────────┘        │
│          │  ┌── [助理回答] ─────────────┐        │
│ [新对话]  │  │  B+树是一种...           │        │
│ ──────── │  │  ┌▼ 参考来源 ────┐        │        │
│ 今天     │  │  │ [1] 文件名    │        │        │
│  记录1   │  │  └──────────────┘        │        │
│          │  └──────────────────────────┘        │
│ 昨天     │  ┌── 第2轮 ─────────────────┐        │
│  记录2   │  │  [用户] 那查找过程呢？      │        │
│          │  │  [助理回答+来源]          │        │
│          │  └──────────────────────────┘        │
│          │  ────────────────────────────        │
│          │  [输入框...]  [↑]  [全部文档▾]        │
└──────────┴──────────────────────────────────────┘
```

### 侧栏收起/展开

#### 隐藏 Streamlit 默认元素（新版本分布较散）

新版 Streamlit（1.35+）将顶部栏元素拆到了多重 DOM 结构中，需逐个隐藏：

```css
#MainMenu {visibility: hidden;}
header {visibility: hidden;}
[data-testid="stHeader"] { display: none !important; }
[data-testid="stSidebarHeader"] { display: none !important; }
[data-testid="collapsedControl"] { display: none !important; }
```

- `[data-testid="stHeader"]` — 新版 Streamlit 的顶层 header 容器（含 toolbar、Deploy、MainMenu）
- `[data-testid="stSidebarHeader"]` — 侧栏顶部的 Streamlit 默认折叠条（含 logo spacer + 折叠按钮）
- `[data-testid="collapsedControl"]` — 内联折叠控制按钮
- 只靠 `header {visibility: hidden}` 不够——新版 Streamlit header 在 visibility 隐藏后仍占布局空间，`display: none` 才能完全移除

#### 侧栏收起/展开

- **展开（280px）**：Logo + 搜索/收起图标按钮 + 新对话按钮 + 历史列表（按时间分组）
- **收起（60px）**：三个图标按钮 ─ ＋(新对话)、🔍(搜索)、▶(展开)
- 通过 `st.session_state.sidebar_collapsed` 布尔值控制
- CSS `transition: width 0.2s ease` 让切换平滑
- 收起状态按钮使用 `st.button(help="xxx")` 实现 hover tooltip 文字标识
- **图标替换方案**：`st.button()` 的 label 是纯文本，不支持 HTML 内嵌图片。正确做法是保持 `st.button(" ", help="xxx")` 调用，通过 CSS 将图标设为按钮的 `background-image`，用 `button[title="xxx"]` 选择器通过 `help` 参数（对应 HTML `title` 属性）精确匹配。PIL 缩放原始大图至 24px → base64 data URL → 嵌入 CSS。**不要用 `<a href="?param=1">` + `st.query_params` 的方式**——该方式因浏览器 URL 导航与 Streamlit SPA 机制的时序冲突，点击经常不触发动作。`st.button()` 是 100% 可靠的 Streamlit 原生点击回调。
- **⚠️ Streamlit 侧栏折叠 transform 陷阱**：Streamlit 内置折叠机制不靠 CSS width，而是通过 `aria-expanded` 属性 + `transform: translateX(-300px)` 将侧栏移出屏幕。自定义 CSS 只改 `min-width`/`max-width` 无法覆盖。修复需三处同时做：
  1. `st.set_page_config(initial_sidebar_state="expanded")` — 阻止首次加载时自动折叠
  2. CSS `section[data-testid="stSidebar"] { transform: translateX(0) !important; }` — 强制覆盖内置 transform，让 sidebar 始终可见
  3. CSS `[data-testid="collapsedControl"] { display: none !important; }` — 隐藏内置折叠按钮，避免两套系统冲突
  4. CSS `[data-testid="stSidebarHeader"] { display: none !important; }` — 隐藏顶部内置折叠条（含 logo spacer + 默认折叠按钮）
  **浏览器调试方法**：打开 DevTools → Computed tab，检查 `transform` 值。如果 `aria-expanded="false"` 且 `transform: translateX(-Npx)`，就是内置折叠机制作的怪。手动设 `transform: translateX(0px)` 可立即恢复——验证后就知道根因。

### 历史记录

- 页面加载时从 MySQL `qa_logs` 一次加载最近 50 条记录 → 缓存到 `st.session_state.history_cache`
- **性能优化**：侧栏列表初始只加载 `id, session_id, question, created_at` 四个轻量字段（不拉 `llm_answer` 等大文本），用 `load_history_list()` 实现。用户点击条目后，再按 `id` 单条加载完整数据 `load_history_detail(row_id)`。可大幅减少首次页面加载的数据量（50 条 × 大文本 vs 50 条 × 4 字段）。
- 选择历史条目 → `load_history_detail(row_id)` 单条查询（零缓存，确保时效）
- 时间标签函数 `_time_label(dt)` 返回：今天 / 昨天 / 7天内 / 30天内
- 选中态：浅琥珀色背景 `#FFF0E0`
- 搜索完成后自动刷新缓存：`load_history_list.clear()` + `st.session_state.history_cache = None` + `st.rerun()`

### 搜索日志弹窗

- 点击侧栏 🔍 按钮打开（展开/收起状态均可用）
- `st.session_state.show_search_modal` 控制显示/隐藏
- 在弹窗中输入文字，实时按问题文本匹配历史记录（`keyword in q.lower()` 大小写不敏感）
- 点击匹配结果 → 设置 `selected_history` + 关闭弹窗 + rerun 跳转

### 搜索结果持久化

（2026-07-07 多轮对话上线后，此机制已废弃，保留供旧版参考）

多轮对话模式下，搜索结果直接以 message 形式存入 `st.session_state.messages`。每条 message 包含 `role`、`content`、`sources`、`timing`、`rewrite_queries`。无需独立的 search_result 缓存。

## 两段式流式输出

关键 UX 特性：
- 阶段1: Reranker 全部评分后，parent_merge 取前 5 个不同 parent，**立即**在页面上展示来源。如有多个 child 同属一个 parent，自动从后续候选补充，保证始终 5 条（全队列不足时除外）。
- 阶段2: LLM 回答逐 token 流式渲染
- 页面布局：左侧 280px 侧栏历史列表 + 右侧主区域搜索/结果（DeepSeek 风格），无独立日志页
- 用户等待感知从 10-15s 降至 ~1s
- CLI 用 SSE `stream=True` 逐 token `print()`
- Web 用 `st.write_stream()` 逐字符渲染
- 完整 UI 风格规范见 `references/ui-style-guide.md`

### 坑：st.write_stream 不渲染 HTML 标签

在 `_prepend_title()` 中直接 yield `<div>` 等 HTML 标签时，`st.write_stream()` 将其作为纯文本显示，不解析为 HTML。
**正确做法**：用 `st.container(border=True)` 包裹 placeholder，在 generator 中只 yield 纯 Markdown 文本。

## 多轮对话（D30）

### 对话状态管理

`st.session_state.messages` 维护 messages 数组 `[{"role":"user"|"assistant", "content":..., "sources":..., "timing":..., "rewrite_queries":...}]`。
每轮请求 LLM 时，通过 `build_conversation_prompt()` 将历史 + 检索结果 + 当前问题一起拼入 prompt。

### 检索策略

`_should_retrieve(query, turn_index)` 决定是否检索：
- 首轮（turn_index==0）→ 必检索
- 含指代词（它/这个/其/上述/该/那些/哪些）→ 检索
- 含书名号（术语名）→ 检索
- 否则 → 纯 LLM 回答（不检索）

### 轮次限制

`MAX_CONVERSATION_TURNS = 8`，超出 FIFO 丢弃最早轮次。

### UI 布局

```
┌─侧栏─┬─聊天区─────────────────────────┐
│ 对话  │ [用户消息]                     │
│ 历史  │ [助理回答+来源+耗时]          │
│ 列表  │ [用户消息]                     │
│      │ [助理回答+来源+耗时]          │
│      │ ──────────────────────────    │
│ [新对话]│ [输入框......] [↑] [文档▾]  │
└──────┴──────────────────────────────┘
```

- 所有消息从上到下按时间排列
- 底部固定输入区（form + submit_button）
- "新对话" 清除 `messages` + `turn_count` 并 rerun

### 习题模式

输入含 `习题/题目/练习/题/真题/考题` 时自动切换：
- `_search_exercises()` 从 MySQL `exercise_index` 表模糊匹配
- 展示：科目 + 章节 + 题干 + 答案 + 解析摘要
- 不经过 ChromaDB 检索 + Reranker 流水线

## 习题索引（MySQL exercise_index 表）

### 表结构

```sql
CREATE TABLE exercise_index (
    id INT AUTO_INCREMENT PRIMARY KEY,
    subject VARCHAR(32),
    chapter INT,
    chapter_title VARCHAR(256),
    section VARCHAR(16),
    section_title TEXT,
    exercise_num VARCHAR(16),     -- 题号 e.g. "01"
    exercise_type VARCHAR(16),    -- "单选" / "综合"
    exercise_text MEDIUMTEXT,     -- 题干全文（含选项）
    answer VARCHAR(32),           -- 答案字母
    solution_text MEDIUMTEXT,     -- 解析全文
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX (subject), INDEX (chapter), INDEX (section)
);
```

### 解析流程

`scripts/build_exercise_index.py`：
1. 读取 4 个清洗后的 .md 文件
2. 识别 `本节试题精选`（习题段）和 `答案与解析`（解析段）
3. 按题型（单选/综合）解析题号 `01. xxx`，选项行 `A. xxx`，答案行 `## 01. D xxx`
4. 通过 `(章节_key, 题型, 题号)` 匹配习题→解析
5. 写入 MySQL（~2,400 条）

### 坑：答案行格式

答案和解析在同一行：`## 01. D 抽象数据类型（ADT）描述了...`
正则：`^##\s+(\d{2,})\.\s*([A-Ea-e]+)\s+(.*)$` → group(1)=题号, group(2)=答案, group(3)=解析正文
不要用 `$` 锚点匹配行尾，因为正文紧随答案字母之后。

## MinerU 王道考研教材清洗

`scripts/clean_王道.py` 处理 4 本 MinerU 导出的 Markdown 教材。

### 清洗项

| 操作 | 规则 |
|------|------|
| 删封面/目录/广告 | 保留第一个 `## 01 第X章` 之后内容 |
| 删图片 | `![image](...)` 整行删除 |
| 压缩空行 | 3+ 空行 → 2 |
| OCR 清洗 | 重复标点、中文内部空格、不可见字符 |
| 特殊标记 | `## *1.1` → `## 1.1`（去星号） |
| 孤立符号 | 保留中文/英文/数字/基本标点/LaTeX $\$ |

### 章节标题合并问题

MinerU 有时将标题和正文合并为一行：
`## 8.3.1 冒泡排序冒泡排序的基本思想是：从后往前...`
→ `section_title` 字段用 TEXT 类型容纳长文本。清洗脚本在保留原始内容的同时额外处理 OCR 空格。

### 输出

输出到 `clean_docs/王道考研/` 下，原始文件不动。4 个文件从 ~2,087,747 → ~1,593,305 字符（-24%）。

## OCR 文本清洗

项目 `docs/` 下有大量 OCR 文本（含多余空格、形近字、乱码符号）。
清洗方案见 `scripts/clean_ocr.py`（纯规则，不调 API）。

用法：
```
python scripts/clean_ocr.py --dry-run        # 预览
python scripts/clean_ocr.py --output ../     # 输出到其他目录
python scripts/clean_ocr.py                  # 原位覆盖
```

清洗内容：不可见字符、重复标点、孤立符号垃圾、中文内部多余空格、行首尾空白。
详细原理和已知局限见 `references/ocr-cleaning.md`。

## 参考文件

| 文件 | 用途 |
|------|------|
| `references/decision-log.md` | 全量决策记录（D1-D27 + 被否方案 + 踩坑汇总），**每次新决策在此追加**（项目内 E:\RAGLearn\references\decision-log.md） |
| `references/ui-style-guide.md` | 完整 UI 风格规范（色板、布局、组件样式、文字规范），**每次界面修改必参照** |
| `references/filter-pitfall.md` | solution 过滤问题的详细分析 |
| `references/ocr-cleaning.md` | OCR 文本清洗的原理和已知局限 |
| `references/parent-child-chunking.md` | 块数爆炸的数字分析 |
| `references/quiz-generation-design.md` | 题库生成设计方案（方案C+D）— solution反推/预生成
| `references/streamlit-icon-button-pattern.md` | Streamlit 图标按钮模式：`st.button(" ", help="xxx")` + CSS `button[title]` background-image（含完整代码片段） |
| `references/exercise-index-bm25.md` | 习题索引表结构、ngram 全文索引、BM25+LIKE 搜索策略、LLM 关键词提取 |

## 已知坑与边界

### UI 默认值与后端检查不一致（翻车史 2026-07-02）

Streamlit 下拉框默认文案改了（"全部"→"全部文档"），但 `do_search()` 中的检查字符串没同步更新，导致每次搜索都带上 `where={"source":"全部文档"}`，搜不到结果。
**教训**：改默认文案时，追踪后端所有 `!= "旧值"` 的比较——搜索可追溯。
**修复**：`"全部"` → `"全部文档"`。

### Streamlit 侧栏折叠 transform 陷阱（2026-07-03）

**现象**：Sidebar HTML 正常渲染、宽度正常、display/visibility/opacity 都正常，但侧栏不可见。
**根因**：Streamlit 内置折叠机制使用 `aria-expanded="false"` + `transform: translateX(-300px)` 将侧栏移出屏幕，而非 CSS width 或 display。
**调试方法**：浏览器 DevTools → 选中 `section[data-testid="stSidebar"]` → Computed tab 看 `transform` 值。在 Console 执行 `document.querySelector('section[data-testid="stSidebar"]').style.transform = "translateX(0px)"` 验证——侧栏立即恢复即确认。
**修复**：三处同时做——
1. `st.set_page_config(initial_sidebar_state="expanded")`
2. CSS `section[data-testid="stSidebar"] { transform: translateX(0) !important; }`
3. CSS `[data-testid="collapsedControl"] { display: none !important; }`
4. CSS `[data-testid="stSidebarHeader"] { display: none !important; }`

**注意**：`initial_sidebar_state` 默认是 `"auto"`，在某些 Streamlit 版本（1.35+）上首次加载可能检测为折叠。即使自定义了 `sidebar_collapsed` 逻辑，仍要和内置折叠机制明确区分——两套系统独立，都会影响侧栏显示。

### solution 块渗入知识库（已修复 2026-07-07 D29）

D29 将知识库模式改为只检 `type=content`（不再包含 solution）。习题模式独立走 MySQL `exercise_index` 表。此问题不再存在。

### 搜索模式的 metadata 设计

MySQL 8.0+ 的 ngram 全文解析器对中文分词默认 n=2（双字分词），适合纯中文搜索但对含符号的词（如 "B+树"、"TCP/IP"）效果差。

**策略：BM25（主） + LIKE（兜底）**：
- BM25 (`MATCH ... AGAINST ... IN BOOLEAN MODE`)：把关键词拆成 2-字 ngram 片段，用 `+<segment>` 表示必须包含
- LIKE (`%keyword%`)：兜底处理 ngram 分词不了的场景
- 结果合并去重，BM25 排在前面

**坑**：BOOLEAN MODE 中的 `+`、`-`、`*` 是操作符，必须从搜索词中移除。同时单字符关键词（如 "B"）在 ngram 模式下无效。见 `_search_exercises()` 中 `re.sub(r'[+\\-*()~@]', '', w)` 的逻辑。

### Streamlit 辅助函数定义顺序（翻车史 2026-07-07）

Streamlit 中 script 按从上到下执行。sidebar 中调用的函数（如 `_clear_conversation()`）必须在 `with st.sidebar:` 块出现之前定义。否则即使 Python 语法检查通过，运行时也会报 `NameError`。

**规则**：所有 `st.sidebar` 中调用的辅助函数移到 sidebar 代码之前，通常在 `st.query_params` 处理之前。避免 "defined after use" 的运行时错误。这类错误在纯 Python 编译检查中不会被发现，只在 Streamlit runtime 才暴露。

### Patch Windows 路径反斜杠双重转义（翻车史 2026-07-07）

编辑 README.md / CHANGELOG.md 等含 Windows 路径的文件时，`patch()` 工具会将 `\` 解释为转义符：

- 你写 `old_string = ".venv\Scripts\python.exe"`，patch 收到的 `\` 被 shell 转义一次 → 在文件中变成 `.venv\\Scripts\\python.exe`
- 再补刀一次 → `.venv\\\\Scripts\\\\python.exe`
- **修复**：先用 `read_file` 确认文件中实际字符数，old_string 和 new_string 中路径用单反斜杠 `\` 而非双反斜杠 `\\`。如果翻倍了，第二次 patch 用 `old_string = "\\\\"` 匹配，`new_string = "\\"` 修复。
- **预防**：先用 `read_file` 查文件中实际路径写法（是 `\` 还是 `\\`），然后在 old_string/new_string 中使用完全相同的副本。

### 提交前务必编译检查 + 运行时检查（翻车史 2026-07-07）

`patch()` 工具可能因 escape-drift 导致缩进损坏。每次改完 app.py（或任何 Streamlit 脚本）后：

```bash
# 1. 编译检查（捕获语法错误和缩进损坏）
python -c "import py_compile; py_compile.compile('app.py', doraise=True); print('OK')"

# 2. 运行时 NameError 检查（import 遗漏，py_compile 查不到）
#    grep 出所有在函数内部使用但未在文件顶部 import 的模块
#    常见遗漏：_search_exercises() 中 re.sub() 依赖 import re
python -c "
import ast, sys
with open('app.py') as f:
    tree = ast.parse(f.read())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    stdlib_top = {'hashlib','json','re','time','uuid','pathlib','ast','datetime'}
    missing = stdlib_top & {n for n in names if n not in dir(__builtins__)}
    # 实际检查依赖文件顶部的 import 语句，这里只做提示
"
```

缩进错误在 Streamlit 中表现为主页面白屏或部分组件不渲染，控制台无明确报错。

### 固定底部输入栏（Streamlit CSS 陷阱 2026-07-07）

需求：输入对话框固定在屏幕底部，不随消息滚动。

**错误方案**：用 `st.markdown('<div class="fixed-bar">', unsafe_allow_html=True)` 包裹表单（开/闭两个 markdown 调用）。行不通——Streamlit 将每个 `st.markdown()` 渲染为独立的 `stMarkdown` DOM 块，开闭 div 跨两个独立容器，浏览器不会正确配对。

**正确方案**：CSS 直接定位到 `[data-testid="stForm"]` 元素。`position: fixed` 使元素脱离文档流，固定在视窗底部。

⚠️ **Streamlit 版本注意**：不同 Streamlit 版本将 `st.form()` 渲染为不同 HTML 元素：
- Streamlit ≤ 1.34（旧版）→ `<form data-testid="stForm">`
- Streamlit ≥ 1.35（新版）→ `<div data-testid="stForm">`

CSS 选择器**必须用** `[data-testid="stForm"]`（不加 `form` 前缀），才能同时兼容两版本。如果用 `form[data-testid="stForm"]`，新版 Streamlit 下选择器匹配不到任何元素，`position: fixed` 不会生效。

```css
/* 主内容下留空间，不被固定表单遮盖 */
.main .block-container {
    padding-bottom: 80px !important;
}
/* 表单脱离文档流固定在底部 — 用 data-testid 属性选择器兼容新旧 Streamlit */
[data-testid="stForm"] {
    position: fixed !important;
    bottom: 0;
    right: 0;
    z-index: 100;
    background: #F9F6F0;
    padding: 4px 16px 12px;
    border-top: 1px solid #E8DED4;
    margin-bottom: 0 !important;
}
```

**动态侧栏配合**：表单的 `left` 值必须匹配侧栏当前宽度（展开 280px / 收起 60px）。侧栏宽度在动态 CSS 块中设置（`st.session_state.sidebar_collapsed` 决定 `sw`），在同一个动态 CSS 块里追加：

```python
if not st.session_state.show_search_modal:
    sw = "60px" if st.session_state.sidebar_collapsed else "280px"
    st.markdown(f"""
    <style>
    section[data-testid="stSidebar"] {{
        min-width:{sw} !important; max-width:{sw} !important;
        transition:all .2s ease;
    }}
    [data-testid="stForm"] {{
        left:{sw} !important;   /* ← 同步侧栏宽度 */
    }}
    </style>
    """, unsafe_allow_html=True)
```

**调试方法**：浏览器 DevTools → Console 执行以下代码验证选择器是否生效：
```javascript
// 检查表单的 computed position
const f = document.querySelector('[data-testid="stForm"]');
console.log(getComputedStyle(f).position);  // 应为 "fixed"，若为 "static" 则选择器有误
// 检查页面上有几个 stForm 元素
console.log(document.querySelectorAll('[data-testid="stForm"]').length);  // 应为 1
```



代码修改必须先列计划并征得用户同意，不能直接改。
任何修改（哪怕一行）都要：展示改动方案 → 等用户说"可以" → 再动手。
记录在 memory 中永久生效。

**黄金规则：每次修改代码后必须立即 git commit，不要堆多个未提交的 patch。**
坏掉一个改动要回滚整批未提交的更改。每改完一块就提交一次，方便对单次改动做 git revert。

**文档优先顺序：** 当一次修改涉及多个文件时，严格按照以下顺序：
1. 先更新文档（ui-style-guide.md, decision-log.md, CHANGELOG.md, README.md）
2. 再修改代码
3. 最后 git 提交

**补充记录文件工作流**（适用于"帮我补充一下记录文件并提交"类任务）：
1. `git log --oneline origin/master..HEAD` — 查出本地领先远程的未记录提交
2. 对比 `CHANGELOG.md` 最后一节日期/标题，确定缺失条目
3. 查 `references/decision-log.md` 结尾，确认决策日志已是最新（只追加不修改已有条目）
4. 逐条编写 CHANGELOG 条目（日期/改动列表/涉及文件/提交信息）
5. 检查 README.md 是否落后于实际功能（核心特色表、CLI 用法、项目结构图）
6. 处理未暂存文件：删旧文件（`git rm`）、加新文件（`git add`）、更新 `.gitignore`
7. `git add → git commit → git push`
8. **坑**：`patch()` 编辑含 Windows 路径的 README/CHANGELOG 时，`\` 会被转义翻倍，见下方 pitfall

UI 修改后必须测试：至少验证一次搜索能正常返回结果（Streamlit 端和 CLI 端各一）。
注意追踪后端检查字符串（如 `!= "全部"` → `!= "全部文档"`）同步更新。

## 改进路线图

`todo.txt` 中按优先级排列了 RAG 精度提升方案。标 ✓ 的已实现：

| 优先级 | 项目 | 状态 |
|--------|------|:----:|
| P0 | 混合检索 (BM25 + Embedding) | ❌ 未动 |
| **P1** | **查询改写 (Query Rewrite)** — LLM 生成 2-3 个同义查询分别检索去重合并 | **✅ 已实现** |
| P2 | 元数据过滤 (Metadata Filtering) | ❌ 未动 |
| **P3** | **父文档检索 (Parent Document Retriever)** — 双层切分，child 检索 parent 送 LLM | **✅ 已实现** |
| P4 | 多轮对话 (D30 方案) | **✅ 已实现** |
| P5 | 习题索引 + 习题模式 | **✅ 已实现** |

同时 `references/filter-pitfall.md` 记录了具体的 solution 过滤问题和分析过程。

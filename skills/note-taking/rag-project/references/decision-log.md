# RAGLearn 项目决策日志

维护此文件的目的：记录整个 RAGLearn 项目开发过程中经过讨论、对比、拍板的重要决策，包括**被否定的方案**和**踩过的坑**。供日后维护、重构、扩展时查阅，避免重复走弯路。

> **原则：** 任何代码修改必须先列计划 → 用户确认 → 再动手。
>
> **更新规则：** 每次做出新决策时，在此文件末尾追加条目（不要修改已有条目）。日期格式 `YYYY-MM-DD`。

---

## D1 — Embedding 模型升级

- **日期：** 2026-06 下旬
- **决策：** 升级为 **bge-large-zh-v1.5（1024 维）**，替代 bge-small-zh-v1.5（384 维）。
- **代价：** 内存/存储增加 ~2.7x，encode 耗时增加 ~50%。
- **涉及文件：** `config.py` — `EMBED_MODEL_PATH`

## D2 — LLM 方案切换

- **日期：** 2026-06 下旬
- **决策：** 选用 **DeepSeek API** 作为默认 LLM，Ollama Qwen2.5-3B 保留为本地备选。
- **理由：** 3B 模型回答质量有限；API 延迟可接受。
- **配置：** `LLM_BASE_URL=https://api.deepseek.com/v1`，`LLM_MODEL=deepseek-v4-flash`

## D3 — Reranker 从 CPU 迁移到 GPU

- **日期：** 2026-06-30
- **决策：** 改为 `device=DEVICE`（自动检测 CUDA）。Rerank 时间从 ~3.2s → ~0.3s。
- **涉及文件：** `query.py`、`app.py`

## D4 — LLM 调用方式：requests 替代 openai 客户端

- **日期：** 2026-06-30
- **根因：** httpx 在 MSYS2/git-bash 下连 localhost 失败（WinError 10061）。
- **决策：** 全部 LLM 调用使用 **`requests.post()` 直调**。
- **涉及文件：** `query.py`、`app.py`

## D5 — Streamlit 端口管理

- **日期：** 2026-06-30
- **发现：** Streamlit 默认绑定 `0.0.0.0`，无需 `--server.address`。
- **决策：** 重启前必杀旧进程：`netstat -ano | grep ':8501' | grep LISTENING` → `taskkill //F //PID <pid>`。

## D6 — 两段式流式输出

- **日期：** 2026-07-01
- **方案：** 阶段1（Rerank 后立即展示来源）+ 阶段2（逐 token 流式回答）。
- **效果：** 用户等待感知从 10-15s 降至 ~1s。

## D7 — 流式渲染逐字拆分

- **日期：** 2026-07-01
- **决策：** token 拆成单字符 yield，`st.write_stream` 渲染平滑。

## D8 — 输入框 Enter 搜索

- **日期：** 2026-07-01
- **决策：** `st.form` + `st.form_submit_button` 替代 `st.text_input` + `st.button`。

## D9 — max_tokens 和 timeout 放宽

- **日期：** 2026-07-01
- **决策：** `max_tokens: 1024 → 2048`，`timeout: 60 → 120`。

## D10 — __path__ 警告压制

- **日期：** 2026-07-01
- **根因：** transformers 在 import 时调用 `warnings.simplefilter("default")` 重置过滤规则。
- **决策：** `filterwarnings` 移到函数内部，紧贴 import 前设置。

## D11 — 移除 exercise 习题存储

- **日期：** 2026-07-03
- **决策：** exercise 段跳过不存，删除 `--practice` 参数。
- **涉及文件：** `ingest.py`、`query.py`、`app.py`

## D12 — 记忆持久化方案

- **日期：** 2026-07 初
- **决策：** 详细上下文存入 `skill:rag-project`，memory 只保留简短索引。

## D13 — 查询改写 (P1) 方案

- **日期：** 2026-07-02
- **决策：** 用同一个 DeepSeek API 做改写；同义扩展风格。
- **涉及文件：** `config.py`（`REWRITE_ENABLED`）、`query.py`（`rewrite_query()`、`multi_retrieve()`、`--no-rewrite`）

## D14 — Parent text 存储位置

- **日期：** 2026-07-02
- **方案对比：** A) 塞 child metadata ✓ / B) 独立 JSON 文件 / C) ChromaDB 第二 collection
- **决策：** 方案 A。项目规模下（~4300 child）简单性远大于重复存储开销。

## D15 — 索引重建必要性

- **日期：** 2026-07-02
- **决策：** 任何 parent-child 改动后必须 `ingest.py --reset` 重建。

## D16 — Parent-Child 切分方案：偏移映射

- **日期：** 2026-07-02
- **根因：** parent 内切 child 导致块数从 ~4300 膨胀到 ~7600（+77%）。
- **决策：** 连续滑动窗口切 child + 独立算 parent + 偏移量映射归属。子决策：parent 切分改用 `RecursiveCharacterTextSplitter(\n\n→\n→。)` 避免教材无 `\n\n` 时产生 3 万字巨块。
- **附带修复：** `retrieve()` 透传 `parent_id`/`parent_text`（之前因 metadata 未保存导致 `parent_merge()` 静默跳过）。
- **涉及文件：** `ingest.py`、`query.py`、`app.py`

## D17 — UI 风格：按钮式导航替代 Tab

- **日期：** 2026-06-30
- **决策：** `st.button` + primary/secondary 类型替代 `st.tabs`。

## D18 — Git 提交规范

- **决策：** Conventional Commits。每次提交必须同步更新 `CHANGELOG.md`。

## D19 — docs/ 目录排除版本管理

- **决策：** .gitignore 排除 `docs/`。`clean_docs/` 也未纳入版本管理。

## D20 — 模型/依赖下载（中国大陆优化）

- **决策：** 模型从 hf-mirror.com / modelscope.cn 下载；PyTorch 从 system site-packages 复制到 venv；pip 用清华镜像。

## D21 — NVIDIA GeForce RTX 4050

- **硬件：** RTX 4050 Laptop GPU，CUDA 12.1。系统 Python 3.12.5 已有 torch 2.5.1+cu121。

## D22 — 分块参数

| 参数 | 值 | 说明 |
|------|:--:|------|
| `CHUNK_SIZE` | 512 | child 块大小（字符） |
| `CHUNK_OVERLAP` | 128 | child 间重叠量 |
| `PARENT_CHUNK_SIZE` | 2000 | parent 块大小（字符） |
| `PARENT_CHUNK_OVERLAP` | 200 | 定义但未使用 |

## D23 — 小样本先验证原则

- **日期：** 项目全周期
- **决策：** 任何批量/全量操作前先用小样本验证。

## D24 — 固定参考来源条数为 5

- **日期：** 2026-07-02
- **决策：** `rerank()` 返回全部评分结果（非仅 Top-5），`parent_merge()` 新增 `target_k` 参数，从全排序队列取前 5 个不同 parent。
- **涉及文件：** `query.py`

## D25 — 参考来源固定为 5 条 + 改写信息展示

- **日期：** 2026-07-02
- **决策：** 页面显示顺序改为：**改写信息 → LLM 回答 → 耗时统计 → 参考来源**。改写信息只显示与原文不同的查询。`multi_retrieve()` 多返回 `display_queries`。
- **涉及文件：** `query.py`、`app.py`

---

## 被否定的方案汇总

| 方案 | 被否原因 | 替代方案 |
|------|---------|---------|
| `openai.OpenAI` 客户端 | httpx Win10061 | `requests.post()` 直调 |
| parent_text 存 JSON 文件 | 两处存储一致性问题 | 塞 child metadata |
| parent_text 存第二个 ChromaDB collection | 多一次查询延迟 | 塞 child metadata |
| 邻近合并（方案 B） | 无语义边界 | 偏移映射（方案 A） |
| 一次遍历映射（方案 A'） | 与连续滑动窗口互斥 | 两阶段 join（方案 A） |
| Tab 导航 | 用户偏好按钮 | 按钮式 navigation |
| Ollama 本地 LLM（作为默认） | 3B 模型质量不足 | DeepSeek API |
| bge-small-zh-v1.5（384维） | 精度不足 | bge-large-zh-v1.5（1024维） |
| Memory 存全部上下文 | 无法按项目隔离 | Skills 系统 |

---

## 踩坑记录

| 坑 | 表现 | 根因 | 解决 |
|----|------|------|------|
| __path__ 警告 | transformers 重置 filter | `simplefilter("default")` 在 import 时调用 | filter 移到函数内部紧贴 import 前 |
| httpx Win10061 | OpenAI 客户端连 localhost 失败 | MSYS2/git-bash 下 httpx socket 异常 | 全用 requests.post() |
| Streamlit 端口冲突 | 4 进程占 8501 | 多次启动未杀旧进程 | netstat + taskkill 清查 |
| parent-child tail loss | ingest 超时，块数 7600 | parent 边界打断 child 滑动窗口 | 偏移映射连续滑动 |
| parent 超大（33K字） | parent_text 巨块 | 教材无 `\n\n` 段落分隔 | RecursiveCharacterTextSplitter 分级切分 |
| parent_merge 静默跳过 | 父文档合并无效果 | retrieve() 不保存 metadata 字典 | 透传 parent_id/parent_text 到 doc dict 顶层 |
| HuggingFace 下载失败 | SSL 错误 | 中国大陆网络环境 | 改用 hf-mirror.com 或 modelscope.cn |
| System Python CUDA torch | venv 需要重复下载 2.3GB | PyTorch 下载慢 | 复制 system site-packages 到 venv |

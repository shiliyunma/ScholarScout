# ScholarScout — 项目规则（AGENTS.md）

> Hermes / 各类 AI 助手读本文件。
> 本正文与 `CLAUDE.md`、`docs/项目规则.md` 三份必须逐字一致，由 `scripts/check_rules_sync.py` 强制；不一致即构建失败。
> 人类阅读版说明见 `docs/03-目录结构与规则文件.md`。

<!-- RULES:BEGIN -->
# 0. 协作铁律（2026-10-05 定稿，违反即拒收）

1. **先定接口、数据模型、目录结构，再让 AI 填实现。** 禁止"帮我写个系统"式开局。
2. **一次只改一个文件或一个函数**，禁止顺手重构整个项目。
3. **规则文件写清楚**（技术栈、命名、错误处理），即本文件。
4. **lint、类型检查、测试、构建，一个不过就不合。**
5. **关键路径人工逐行 review**（清单见 §5）。
6. **新增依赖要审批**，AI 最爱随手 import 不存在的包。
7. **小 PR、勤提交、能回滚。**
8. **定期重构**，AI 写得快，技术债也快。
9. **职责划分先于编码**：动手前先定"这属于哪一层、叫什么名字、放哪个目录"，再填实现 ——
   禁止先写一个文件包揽全部职责、事后再拆（拆分是补救，不是流程）。一个文件只承担一类职责；
   跨层 import 由 `tests/test_layering.py` 在门禁里拦下。

# 1. 技术栈（锁定；变更必须先改本文件并单独提交）

| 面 | 选型 | 说明 |
|---|---|---|
| 语言 | Python 3.11 | 本机 3.11.5；不用 3.12+ 专属语法 |
| 包管理 | uv | 不用 pip 直装，不用 conda |
| 桌面壳 | pywebview | Windows 走系统 Edge WebView2 |
| 前端 | 原生 HTML/CSS/JS | 无构建步骤、无 npm、无框架 |
| 前后端桥 | JSON-RPC 2.0 over stdio | 见 docs/01-接口契约.md |
| 存储 | SQLite(WAL) + FTS5 | 单文件 run/state.db，stdlib sqlite3 |
| 模型 | OpenAI 兼容 HTTP 接口 | DeepSeek 与本地 Ollama 同一套代码 |
| 测试 / lint / 类型 | pytest / ruff / mypy | 由 scripts/check.sh 统一跑 |

**明确禁止**（本课题方向决策，不是偏好）：
- 向量库 / embedding / 语义检索 —— 本课题不做 RAG，走 FTS5 + 开放世界检索
- ORM —— 直接写 SQL，模型见 docs/02
- Electron / Node 前端 —— 已评估否决（成本过高）
- web 框架（FastAPI/Flask）—— 桌面应用不需要
- 任何"顺手加"的框架

# 2. 命名

| 对象 | 规则 | 例 |
|---|---|---|
| 模块 / 包 | snake_case | `research_search.py` |
| 类 | PascalCase | `SourceCard` |
| 函数 / 变量 | snake_case | `upsert_source` |
| 常量 | UPPER_SNAKE | `MAX_CHUNK_CHARS` |
| 工具名（模型可见） | `namespace.action` | `research.search` |
| 工具 handler | `namespace_action` | `research_search` |
| 表 / 列 | 表复数 snake_case，列单数 | `doc_chunks.text_seg` |
| 记录 id | 前缀 + 8 位 base32 | `src_7fk2m9qa` |
| 测试文件 / 函数 | `test_<模块>.py` / `test_<行为>_<条件>` | `test_fts_update_removes_old_text` |
| 分支 | `feat/<slug>` `fix/<slug>` | `feat/research-search` |

**反模式（不要写）**：`utils.py` / `helpers.py` / `common.py` 垃圾场；在 `__init__.py` 里 re-export 挪过位置的符号；单文件超过 600 行（应按主题拆）；单函数超过 60 行。

# 3. 错误处理

1. **禁止裸 `except:` 与 `except Exception: pass`。** 捕获具体异常并说明恢复策略。
2. **外部边界必须转成契约错误码**（抓取、RPC、解析），错误码表见 docs/01 §2。
3. **工具 handler 永不向模型抛异常**：捕获后返回 `{ok:false, error:{code,message}}`，让模型能换路或重试。
4. **内部错误 fail loud**：库层出错直接抛，由 loop 收敛成 `stop_reason="error"`，不静默吞。
5. **重试只写在 `fetch/http.py`**，其他层不许自己写 retry。
6. **所有网络调用必须带超时**（默认 20s），禁止无超时等待。
7. 日志用 `logging` 并带 `session_id` / `turn_id`；**禁止用 `print` 当日志**。
8. **落盘路径必须经 `paths.py`** 拼接并断言在 `data/` 之下（防 `../` 逃逸）。

# 4. 依赖政策

- 运行期依赖白名单：`deps-allowlist.txt`；`scripts/check_new_deps.py` 比对 `pyproject.toml`，**未审批的新依赖 = 构建失败**。
- 申请新增依赖必须写清：用途 / 为什么 stdlib 不行 / 体积 / 许可证 / 近半年是否维护。
- 版本一律带上界：`>=floor,<next_major`。
- 已批准运行期依赖：`httpx`、`PyYAML`、`pywebview`、`pymupdf`。
- 禁止：向量库、embedding、ORM、web 框架、任何 Node 依赖。

# 5. 门槛与关键路径

**门槛（`scripts/check.sh`，一个不过就不合）**

```bash
ruff check src tests scripts eval
ruff format --check src tests scripts eval
mypy src eval
pytest -q
python scripts/check_rules_sync.py
python scripts/check_new_deps.py
python scripts/scout.py --check-ui
python scripts/make_code_map.py --check
```

**关键路径 = 必须人工逐行 review 的文件（铁律 #5）**

| 路径 | 风险 |
|---|---|
| `src/scholarscout/state/migrations/*.sql` | 数据迁移不可逆 |
| `src/scholarscout/state/db.py` | 唯一写库入口 |
| `src/scholarscout/fetch/http.py` | 出网唯一入口（SSRF / 挂死 / 封禁） |
| `src/scholarscout/fetch/html_text.py`、`pdf_text.py` | 解析第三方输入（注入 / 爆内存） |
| `src/scholarscout/providers/*` | 限速与密钥使用 |
| `src/scholarscout/runtime/loop.py` | 工具次数上限、取消、stop_reason |
| `src/scholarscout/grading/tiers.py` | 论文指标的分母 |
| `eval/*` | 计分逻辑 |

# 6. 提交与 PR

- 一次提交只做一件事；**改接口必须先单独提交 `docs/01-接口契约.md`**，再提交实现。
- 提交信息：`type(scope): 说明`，type ∈ feat / fix / docs / refactor / test / chore / perf。
- **不提交** `data/`、`run/`、`*.db`、`.env`。
- 每个分支末尾必须能通过 `scripts/check.sh`。
- 出问题优先 `git revert`，不要手改历史。
<!-- RULES:END -->

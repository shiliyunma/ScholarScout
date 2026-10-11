# state/ —— 存储层：唯一碰 SQL 的地方

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的规定组织（该文要求
> 一个好的 README 回答四件事），只写**不随代码变的那部分**：这一层是干什么的、先看哪个文件、
> 哪些是 API、去哪看更多。
>
> **行数、文件清单、导出符号、依赖方向刻意不写在这里** —— 它们会随代码腐烂，
> 一律由 `scripts/make_code_map.py` 生成到 `docs/代码地图/00-符号清单.md`。
> 表结构也不在这里复述（"Duplication is evil"），权威副本见 `docs/02-数据模型.md`。

## 1. 这个目录放什么

把 `run/state.db`（SQLite WAL + FTS5）的**读写**收成一处：
`state/*` 是本项目**唯一允许出现 `execute(` 的地方**，其他模块要到库里取东西，都得经这里的函数。

- **唯一写库入口**是 `db.py` 的 `Database` 门面：连接参数（WAL / foreign_keys / busy_timeout /
  synchronous）只在这里设；业务模块不许自己 `sqlite3.connect`。
- 其余文件按主题切：会话、工具轨迹、入库、资料库只读查询、检索、学习产物、飞书配对、导出。
- **子目录 `migrations/`**：数据库 DDL 的增量脚本（`NNNN_*.sql`）。它有自己的 `README.md`
  说明"只增不改"的纪律与命名规则 —— 改库结构前先读那一份。
- **正文（全文）不入库**：库里只放摘要与路径（`raw_path` / `text_path`），检索（FTS5）只覆盖
  `summaries` 与 `messages` 两张表。完整口径见 `docs/02-数据模型.md` §4。

## 2. 先看哪个文件

1. **`db.py`** —— `Database` 门面（唯一写库入口，**关键路径，人工逐行 review**）。先读它，
   因为所有连接与会话/事务都经它。
2. **`migrate.py`** —— 迁移执行器（把 `migrations/NNNN_*.sql` 按版本号依次应用，`schema_version`
   记账）。它决定 `migrations/` 里的文件是怎么被用起来的。
3. **`ingest.py`** —— 写入仓储：来源 / 文档 / 摘要 / 出处（去重与幂等都在这里）。
4. **`sessions.py`** —— 会话与消息仓储（`idx` 分配、会话标题、分页）；`new_id()` 是项目里
   id 前缀的唯一来源。
5. **`search.py`** —— 检索封装：FTS5(trigram) 查询 + 短查询 LIKE 兜底（两条路径返回同一结构）。
6. **`learning.py`** —— 学习产物门面；实现按域拆进 `learning_topics.py` / `learning_routes.py` /
   `learning_cards.py`（外部一律从 `learning` 导入，入口名不动）。
7. 其余：`traces.py`（轨迹/用量/来源状态）、`archive.py`（资料库只读）、`pairing.py`（飞书配对
   与白名单）、`export_session.py` / `export_html.py`（导出）、`initdb.py`（建库入口）。

## 3. 哪些是 API（外部可依赖的契约）

调用方在 `rpc/`、`runtime/`、`tools/`、`library/` 与 `tests/state/`。改签名 = 破坏契约。

- `db.py`：`class Database`（`execute` / `executescript` / `query` / `query_one` / `scalar` /
  `scalar_int` / `scalar_str` / `transaction` / `table_names` / `trigger_names` / `has_table` /
  `counts` / `conn` / `close`）与模块级 `DOMAIN_TABLES`
- `migrate.py`：`migrations_dir()` / `now_iso()` / `discover(directory)` / `current_version(db)` /
  `apply_migrations(db, *, target, directory)`
- `initdb.py`：`init_db(db_path, *, target, make_dirs)` / `main(argv)`
- `sessions.py`：`new_id(prefix)` / `auto_title(content)` / `create_session(...)` /
  `get_session(db, session_id)` / `get_session_by_key(db, session_key)` / `ensure_session(...)` /
  `list_sessions(db, *, limit, offset)` / `append_message(...)` / `list_messages(db, session_id,
  *, limit, before_idx)` / `count_messages(db, session_id)` / `rename_session(...)` /
  `set_title_if_unnamed(...)` / `retitle_if_unnamed(...)` / `set_pinned(...)` / `delete_session(...)`
- `ingest.py`：`upsert_source(...)` / `save_document(...)` / `save_summary(...)` /
  `record_mention(...)` / `latest_summary(db, doc_id)` / `latest_summary_for_source(...)` /
  `document_for_source(...)` / `save_grade(...)` / `apply_tier_to_source(...)` /
  `latest_grade(...)` / `list_grades(...)`
- `traces.py`：`digest(payload)` / `record_trace(...)` / `list_traces(...)` / `record_usage(...)` /
  `record_provider_status(...)`
- `archive.py`：`list_documents(...)` / `get_document(db, doc_id)` / `document_paths(...)` /
  `display_format(url, raw_path)` / `read_text(...)`
- `search.py`：`class Hit` / `normalize_query(query)` / `query_path(query)` /
  `search_summaries(db, query, *, limit)` / `search_messages(db, query, *, session_id, limit)` /
  `search_all(db, query, *, limit)`
- `learning.py`（门面，再导出子模块符号）：`save_note` / `get_note` / `list_notes` /
  `notes_for_source` / `parse_cards` / `grade_card` / `review_card` / `due_cards` /
  `card_stats` / `note_card_counts` / `save_route` / `get_route` / `latest_route` / `list_routes` /
  `revise_route` / `save_topic_map` / `get_topic_map` / `latest_topic_map` / `list_topic_maps`
- `pairing.py`：`create_code` / `claim_code` / `verify_code` / `approve_user` / `is_approved` /
  `approved_users` / `revoke_user` / `pending_count` / `list_pending` / `set_temp_open` /
  `temp_open` / `locked_out` / `clear_expired` / `class VerifyResult`
- `export_session.py`：`build_markdown(...)` / `export_to_file(...)`；
  `export_html.py`：`build_html(...)` / `export_html_to_file(...)`

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- **DDL 的唯一权威来源**：`docs/02-数据模型.md`（迁移文件是"执行体"，本文件是"说明与权威"）。
- 分层与模块边界：`docs/03-目录结构与规则文件.md` §2；门禁 `tests/test_layering.py`
  （规则表里 `state` 不许 import `runtime` / `rpc` / `desktop` / `tools`）。
- 接口契约（RPC 方法用的字段）：`docs/01-接口契约.md`。
- 测试：`tests/state/`（schema / fts / search_path / ingest / 会话标题 / 抽背卡 / 配对 / 导出 …）。
- 符号与文件清单：`docs/代码地图/00-符号清单.md`。

## 改这个目录之前先知道

- **`state/*` 是唯一碰 SQL 的地方**；反过来 `providers/*` **不许** import `state/*`（取数据与落库分开）。
- `Database.conn` 是**逃生舱**，只在迁移执行器里用 —— 业务代码不许碰。
- 连接是 `isolation_level=None`（自动提交）；要原子性就用 `db.transaction()`，别手写 `BEGIN`。
- **时间统一经 `migrate.now_iso()`**（带微秒、带时区）—— 同一秒内可能写多行，秒级精度会让
  `ORDER BY created_at DESC` 取到非确定的"最新一条"。
- **摘要多版本共存**是刻意的：取"当前摘要"用 `ORDER BY created_at DESC, rowid DESC`。
- **改库结构走新增迁移**（`migrations/0004+`），不改历史迁移文件 —— 见 `migrations/README.md`。
- **关键路径**（`AGENTS.md` §5）：`state/db.py` 与 `state/migrations/*.sql` 必须人工逐行 review。

-- ScholarScout 0001_init.sql
-- 全部 DDL 的唯一执行体。权威说明见 docs/02-数据模型.md（v0.2）。
-- 结构变更请新增 NNNN_*.sql，**不要改本文件**（已应用的迁移改了就与历史库不一致）。
-- PRAGMA journal_mode / foreign_keys 由 state/db.py 在连接时设置，不放在这里。

-- ============================================================ 元信息

CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state_meta (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- ============================================================ 来源与分层

CREATE TABLE IF NOT EXISTS source_tier_rules (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  match_type TEXT NOT NULL CHECK (match_type IN ('domain','domain_suffix','url_regex','source_type')),
  pattern    TEXT NOT NULL,
  tier       INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 4),
  weight     REAL NOT NULL DEFAULT 1.0,
  note       TEXT,
  enabled    INTEGER NOT NULL DEFAULT 1,
  UNIQUE (match_type, pattern)
);

CREATE TABLE IF NOT EXISTS sources (
  id            TEXT PRIMARY KEY,
  url           TEXT NOT NULL,
  url_canonical TEXT NOT NULL UNIQUE,
  domain        TEXT NOT NULL,
  title         TEXT,
  authors_json  TEXT,
  year          INTEGER,
  venue         TEXT,
  doi           TEXT,
  source_type   TEXT NOT NULL,
  tier          INTEGER,
  authority     REAL,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  meta_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_sources_domain ON sources(domain);
CREATE INDEX IF NOT EXISTS idx_sources_tier   ON sources(tier);
CREATE INDEX IF NOT EXISTS idx_sources_year   ON sources(year);
CREATE INDEX IF NOT EXISTS idx_sources_type   ON sources(source_type);

-- ============================================================ 文档（正文落文件，不入库）

CREATE TABLE IF NOT EXISTS documents (
  id             TEXT PRIMARY KEY,
  source_id      TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  title          TEXT,
  abstract       TEXT,
  sha256         TEXT NOT NULL,
  mime           TEXT,
  lang           TEXT,
  chars          INTEGER NOT NULL DEFAULT 0,
  raw_path       TEXT,
  text_path      TEXT,
  extract_status TEXT NOT NULL DEFAULT 'pending',
  extract_engine TEXT,
  truncated      INTEGER NOT NULL DEFAULT 0,
  fetched_at     TEXT NOT NULL,
  http_status    INTEGER,
  ok             INTEGER NOT NULL DEFAULT 1,
  error          TEXT,
  UNIQUE (source_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);
CREATE INDEX IF NOT EXISTS idx_documents_sha    ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(extract_status);

-- ============================================================ 摘要（本项目核心表）

CREATE TABLE IF NOT EXISTS summaries (
  id              TEXT PRIMARY KEY,
  doc_id          TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  source_id       TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  session_id      TEXT,
  kind            TEXT NOT NULL CHECK (kind IN ('paper','doc','repo','web','scanned','degraded')),
  title_zh        TEXT,
  summary_short   TEXT NOT NULL DEFAULT '',
  points_json     TEXT,
  why_relevant    TEXT,
  model           TEXT,
  prompt_version  TEXT NOT NULL,
  input_chars     INTEGER NOT NULL DEFAULT 0,
  input_truncated INTEGER NOT NULL DEFAULT 0,
  degraded        INTEGER NOT NULL DEFAULT 0,
  guard_failed    INTEGER NOT NULL DEFAULT 0,
  error           TEXT,
  created_at      TEXT NOT NULL,
  UNIQUE (doc_id, prompt_version)
);
CREATE INDEX IF NOT EXISTS idx_summaries_doc    ON summaries(doc_id);
CREATE INDEX IF NOT EXISTS idx_summaries_source ON summaries(source_id);
CREATE INDEX IF NOT EXISTS idx_summaries_ver    ON summaries(prompt_version);

-- ============================================================ 会话与工具轨迹

CREATE TABLE IF NOT EXISTS sessions (
  id                 TEXT PRIMARY KEY,
  session_key        TEXT UNIQUE,
  title              TEXT,
  source             TEXT NOT NULL DEFAULT 'desktop',
  model              TEXT,
  system_prompt_hash TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  ended_at           TEXT,
  meta_json          TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
  id              TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  idx             INTEGER NOT NULL,
  role            TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool')),
  content         TEXT NOT NULL DEFAULT '',
  tool_calls_json TEXT,
  tool_call_id    TEXT,
  name            TEXT,
  active          INTEGER NOT NULL DEFAULT 1,
  token_in        INTEGER,
  token_out       INTEGER,
  created_at      TEXT NOT NULL,
  UNIQUE (session_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, idx);

CREATE TABLE IF NOT EXISTS tool_traces (
  id            TEXT PRIMARY KEY,
  session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  message_id    TEXT,
  turn_id       TEXT NOT NULL,
  step          INTEGER NOT NULL,
  tool_name     TEXT NOT NULL,
  args_json     TEXT,
  args_digest   TEXT,
  result_digest TEXT,
  result_path   TEXT,
  ok            INTEGER NOT NULL,
  error         TEXT,
  latency_ms    INTEGER,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_session ON tool_traces(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_traces_tool    ON tool_traces(tool_name);
CREATE INDEX IF NOT EXISTS idx_traces_turn    ON tool_traces(turn_id, step);

CREATE TABLE IF NOT EXISTS usage_records (
  id                TEXT PRIMARY KEY,
  session_id        TEXT,
  turn_id           TEXT,
  model             TEXT NOT NULL,
  purpose           TEXT,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  est_cost          REAL,
  created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_status (
  id          TEXT PRIMARY KEY,
  provider    TEXT NOT NULL,
  at          TEXT NOT NULL,
  ok          INTEGER NOT NULL,
  http_status INTEGER,
  latency_ms  INTEGER,
  note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_provider_status ON provider_status(provider, at DESC);

-- ============================================================ 出处关联

CREATE TABLE IF NOT EXISTS source_mentions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id  TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  doc_id     TEXT REFERENCES documents(id) ON DELETE SET NULL,
  summary_id TEXT REFERENCES summaries(id) ON DELETE SET NULL,
  session_id TEXT NOT NULL,
  message_id TEXT,
  turn_id    TEXT,
  role       TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mentions_source  ON source_mentions(source_id);
CREATE INDEX IF NOT EXISTS idx_mentions_session ON source_mentions(session_id, created_at);

-- ============================================================ 学习产物

CREATE TABLE IF NOT EXISTS topic_maps (
  id         TEXT PRIMARY KEY,
  session_id TEXT,
  topic      TEXT NOT NULL,
  graph_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topic_maps_topic ON topic_maps(topic);

CREATE TABLE IF NOT EXISTS learning_routes (
  id           TEXT PRIMARY KEY,
  session_id   TEXT,
  topic_map_id TEXT REFERENCES topic_maps(id) ON DELETE SET NULL,
  topic        TEXT NOT NULL,
  level        TEXT,
  weeks        INTEGER,
  plan_json    TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_routes_topic ON learning_routes(topic, created_at DESC);

CREATE TABLE IF NOT EXISTS notes (
  id         TEXT PRIMARY KEY,
  session_id TEXT,
  kind       TEXT NOT NULL CHECK (kind IN ('summary','note','card')),
  title      TEXT,
  content    TEXT NOT NULL,
  refs_json  TEXT,
  tags_json  TEXT,
  path       TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_kind ON notes(kind, created_at DESC);

-- ============================================================ 检索索引（只有两张，trigram）
-- 决议 6：只检索摘要与会话记录。正文不入库、不进 FTS。
-- trigram 要求查询 >= 3 字符；短查询由 state/search.py 走 LIKE 兜底（见 docs/02 §4.1）。

CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
  title_zh, summary_short, points_json,
  content='summaries', content_rowid='rowid',
  tokenize='trigram'
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  content,
  content='messages', content_rowid='rowid',
  tokenize='trigram'
);

-- ============================================================ 触发器（索引自动同步）

CREATE TRIGGER IF NOT EXISTS summaries_fts_ai AFTER INSERT ON summaries BEGIN
  INSERT INTO summaries_fts(rowid, title_zh, summary_short, points_json)
  VALUES (new.rowid, new.title_zh, new.summary_short, new.points_json);
END;
CREATE TRIGGER IF NOT EXISTS summaries_fts_ad AFTER DELETE ON summaries BEGIN
  INSERT INTO summaries_fts(summaries_fts, rowid, title_zh, summary_short, points_json)
  VALUES ('delete', old.rowid, old.title_zh, old.summary_short, old.points_json);
END;
CREATE TRIGGER IF NOT EXISTS summaries_fts_au AFTER UPDATE ON summaries BEGIN
  INSERT INTO summaries_fts(summaries_fts, rowid, title_zh, summary_short, points_json)
  VALUES ('delete', old.rowid, old.title_zh, old.summary_short, old.points_json);
  INSERT INTO summaries_fts(rowid, title_zh, summary_short, points_json)
  VALUES (new.rowid, new.title_zh, new.summary_short, new.points_json);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
  INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;

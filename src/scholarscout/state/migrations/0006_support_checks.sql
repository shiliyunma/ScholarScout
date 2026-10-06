-- 引用语义支持度校验结果（"这条资料真的支持这句话吗"）。
-- 与"出处覆盖率"互补：覆盖率管"有没有挂上真实来源"，本表管"挂上的来源是否支持该说法"。
--
-- 为什么落库而不是只出报告：
--   ① 可复算、可累积 —— 同一批产物重复审不重复计费；
--   ② 弱支持清单可直接交给人工复核（论文里的抽样核对）；
--   ③ prompt_version 记录用的是哪版判定提示词，保证历史可比。
CREATE TABLE IF NOT EXISTS support_checks (
  id              TEXT PRIMARY KEY,
  artifact_kind   TEXT NOT NULL,          -- topic_map | route
  artifact_id     TEXT NOT NULL,
  session_id      TEXT,
  item_id         TEXT NOT NULL,          -- 节点 nid / 条目 week-index
  claim           TEXT NOT NULL,          -- 被审的断言原文
  source_ids_json TEXT,                   -- 该条目引用的来源（最多取前 N 条作证据）
  evidence_level  TEXT NOT NULL,          -- summary（有摘要＝强证据）| title（仅标题＝弱证据）
  verdict         TEXT NOT NULL,          -- supported | partial | unsupported | unknown
  reason          TEXT,                   -- 模型给的一句话理由（人工复核用）
  model           TEXT,
  prompt_version  TEXT,                   -- 判定提示词的内容哈希
  created_at      TEXT NOT NULL,
  UNIQUE (artifact_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_support_artifact ON support_checks(artifact_kind, artifact_id);
CREATE INDEX IF NOT EXISTS idx_support_verdict  ON support_checks(verdict);

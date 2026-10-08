-- 抽背复习的**追加式证据日志**（③）。
--
-- 为什么在 card_reviews 之外再记一份：card_reviews 是"每张卡一行"的汇总（重复评 = 更新），
-- 省空间但**不可回放** —— 一旦排期算错，历史就找不回来了，也没法审计"这个间隔是怎么来的"。
-- 有了这份日志：排期 = 对日志的 fold（learning.replay_card），随时能重算、能对账。
--
-- 口径：
--   - 只追加、不修改不删除；一条 = 一次评价；
--   - 只记迁移之后的复习：更早的汇总行没有对应日志，replay 的 logged 会小于 reviews，
--     这不是错，是"这段历史没有留痕"（如实暴露，不假装完整）；
--   - 存 reviewed_at 而不是存算好的间隔：间隔是**派生值**，派生值不进证据。
CREATE TABLE IF NOT EXISTS card_review_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  note_id     TEXT NOT NULL,
  card_index  INTEGER NOT NULL,
  rating      TEXT NOT NULL CHECK (rating IN ('good', 'fuzzy', 'bad')),
  reviewed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_card_review_log_card
  ON card_review_log (note_id, card_index, id);

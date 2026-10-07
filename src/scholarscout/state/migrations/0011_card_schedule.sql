-- B6 间隔重复：给抽背记账表补上"下次什么时候再练"。
--
-- 为什么加在 card_reviews 上而不是新开一张表：排期是"这张卡最近一次复习的结果"派生出来的，
-- 分两张表就会出现"复习记录和排期不一致"这种没法自证的中间态。主键仍是 (note_id, card_index)。
--
-- 口径：ease/interval_days/streak/lapses 全部可空（老行没有），`due_at` 为空时**算到期**
-- （宁可多练一次，也不让一张没有排期的卡永远沉在库里 —— 见 learning/srs.py::is_due）。
ALTER TABLE card_reviews ADD COLUMN ease REAL;
ALTER TABLE card_reviews ADD COLUMN interval_days REAL;
ALTER TABLE card_reviews ADD COLUMN streak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE card_reviews ADD COLUMN lapses INTEGER NOT NULL DEFAULT 0;
ALTER TABLE card_reviews ADD COLUMN due_at TEXT;

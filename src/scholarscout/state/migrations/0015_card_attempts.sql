-- 每题每次作答都留痕（大浩选定「可回看历史」）。
-- 复习日志原本只记 rating（档位），现在把「当时答了什么、对不对」也记下来。
-- SQLite 的 ADD COLUMN 是纯增量：老行给 NULL，不破坏既有数据。
ALTER TABLE card_review_log ADD COLUMN response TEXT;
ALTER TABLE card_review_log ADD COLUMN correct INTEGER;

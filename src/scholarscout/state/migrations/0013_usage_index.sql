-- usage_records 上加索引。
--
-- 为什么现在补：`session.usage`（界面每轮都看用量）按 session_id 聚合，
-- 而这张表之前一个索引都没有，等于每次全表扫。数据量现在还小、体感不出来，
-- 但"按会话查用量"是这张表的**唯一**读法 —— 索引属于口径正确性，不只是性能。
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_records (session_id);

-- 会话置顶（左侧栏三点菜单的「置顶」）。
-- 默认 0：老会话自动就是未置顶，不需要回填。
ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;

-- 列表排序用：置顶优先，其次按最近更新。
CREATE INDEX IF NOT EXISTS idx_sessions_pinned ON sessions(pinned DESC, updated_at DESC);

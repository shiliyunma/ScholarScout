-- D2 跨会话记忆：关于用户的**稳定事实**（方向/水平/偏好/约束）。
--
-- 为什么要落库而不是每次从会话里现推：现推意味着每轮都得重读历史（贵且会被裁剪），
-- 而且"第 5 轮才想起他不喜欢深色主题"这种体验不该发生。
--
-- 为什么必须有删除入口：自动记忆**没有出口就是污染** —— 一次误抽（把"我在做毕设"抽成
-- "他要我替他写毕设"）会一直污染之后所有轮次，用户还找不到地方改。所以：
--   · 每条都能删、能改（memory.remove / memory.update）；
--   · 抽取只认"稳定事实"，一次性任务细节被提示词明确排除；
--   · 入库前按相似度判重（同一个偏好不会被反复追加成十条）。
CREATE TABLE IF NOT EXISTS user_facts (
  id             TEXT PRIMARY KEY,
  text           TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('preference', 'fact', 'goal')),
  source_session TEXT,                       -- 哪次会话抽出来的（便于回溯；会话删了留空）
  created_at     TEXT NOT NULL,
  updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_facts_kind ON user_facts(kind, created_at DESC);

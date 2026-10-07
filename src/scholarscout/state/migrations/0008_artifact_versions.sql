-- 产物版本：增量修改时把**上一版**存档，主表始终保留最新版。
--
-- 为什么要有版本而不是就地覆盖：
--   增量修改是"只改几处"，可一旦改错，用户就没法退回上一版 —— 而路线是花了钱生成的。
--   所以每次修改都先归档旧版、再把新版写进主表，`artifact_versions` 就是"可回退的账本"。
--
-- 为什么用 (kind, artifact_id, version) 做联合主键而不是单独一张 route 表：
--   主题地图（map）以后也要走同一条路，届时不需要再建表、也不需要改查询。
--
-- 口径：`version` 记录的是**被存档的那一版**（存档时的版本号），主表行的 `version` 是当前版。
CREATE TABLE IF NOT EXISTS artifact_versions (
  kind         TEXT NOT NULL CHECK (kind IN ('route', 'map')),
  artifact_id  TEXT NOT NULL,
  version      INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  note         TEXT,                    -- 这次修改的一句话说明（来自模型的 summary）
  created_at   TEXT NOT NULL,
  PRIMARY KEY (kind, artifact_id, version)
);

-- 主表加两列：当前版本号 + 最后修改时间（老行默认第 1 版）
ALTER TABLE learning_routes ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE learning_routes ADD COLUMN updated_at TEXT;

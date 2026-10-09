-- 飞书接入向导（docs/18）的配对码与白名单。
--
-- 背景：Hermes 的 DM 配对是"机器人给未知用户发码 → 主人在终端 approve"
-- （gateway/pairing.py:427-479 / hermes_cli/pairing.py:56），码规格 8 位、
-- 字母表剔掉 0/O/1/I（pairing.py:29-30）、只存加盐 SHA-256（pairing.py:413-414）。
-- 本项目的向导场景**顺序相反**：向导先生成"不带身份"的码，用户把码发给机器人，
-- 渠道层捕获发送者 openid 并绑到该码上（claim），最后才批准 —— 这样 openid 自动落库。
--
-- 为什么两张表而不是三张：
--   feishu_pairing_codes  —— 待批码（码 -> 身份），只存哈希，键是随机 entry_id；
--   feishu_paired_users   —— 批准后的白名单（身份 = 飞书 openid）。
-- 「临时放行」开关不另开表，借已有的 state_meta（key='feishu_temp_open'）——
-- 它本来就承担"这类零散状态"的职责（docs/02 §2）。
--
-- 不进 DOMAIN_TABLES：那十张是**研究数据**（sources/documents/...），健康计数据此统计；
-- 配对是**入口层状态**，与 state_meta 同类，不占研究数据的分母。

CREATE TABLE IF NOT EXISTS feishu_pairing_codes (
  entry_id   TEXT PRIMARY KEY,          -- secrets.token_hex(8)，**不暴露码本身**
  code_hash  TEXT NOT NULL,             -- sha256(salt + code)
  salt       TEXT NOT NULL,             -- 16 字节随机盐的 hex
  user_id    TEXT NOT NULL DEFAULT '',  -- 飞书 openid（ou_...）；由 claim 回填，空 = 还没绑定
  user_name  TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL              -- ISO-8601 带时区（项目统一时间格式）
);
CREATE INDEX IF NOT EXISTS idx_feishu_pairing_created ON feishu_pairing_codes(created_at);

CREATE TABLE IF NOT EXISTS feishu_paired_users (
  user_id     TEXT PRIMARY KEY,         -- 飞书 openid（ou_...）
  user_name   TEXT NOT NULL DEFAULT '',
  approved_at TEXT NOT NULL,
  source      TEXT NOT NULL DEFAULT 'pairing'   -- pairing | manual
);

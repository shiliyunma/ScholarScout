-- ScholarScout 0003：补分层规则（E1 评测集跑出来的真实缺口）
--
-- 背景（docs/05 §3.1）：E1 第一次跑出 3 条 tier 不一致，原因两类：
--   1) 域名规则不覆盖子域 —— 已改代码修（grading/tiers.py:domain 匹配同时认子域），本文件不涉及；
--   2) 中文站点后缀缺失 —— .gov.cn / .org.cn 是真实域名，但 .gov 后缀匹配不上 www.gov.cn，
--      于是一整类中国政务/机构站点被漏判成 tier 4。
-- 规则是数据，补数据不改代码。结构变更请新增 0004+，不要改本文件。

INSERT OR IGNORE INTO source_tier_rules (match_type, pattern, tier, weight, note) VALUES
  ('domain_suffix', '.gov.cn',  2, 0.8, '中国政府站点（.gov 匹配不到 gov.cn）'),
  ('domain_suffix', '.org.cn',  3, 0.5, '中国机构/协会站点，权重降低（门槛很低）'),
  ('domain_suffix', '.ac.uk',   2, 1.0, '英国高校/科研站点'),
  ('domain_suffix', '.edu.hk',  2, 1.0, '香港高校站点');

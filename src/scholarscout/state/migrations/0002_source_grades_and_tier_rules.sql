-- ScholarScout 0002：判权所需的存储
--  1) source_grades：判权结果**可复现地**留痕（命中了哪条规则、哪些信号、哪版提示词）
--  2) source_tier_rules 的默认种子：分层是**数据**，不是模型自由心证
-- 权威说明见 docs/02-数据模型.md §3.1 / §7。结构变更请新增 0003+，不要改本文件。

CREATE TABLE IF NOT EXISTS source_grades (
  id              TEXT PRIMARY KEY,
  source_id       TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  topic           TEXT,
  tier            INTEGER NOT NULL,
  tier_label      TEXT NOT NULL,
  authority       REAL NOT NULL,
  relevance       REAL,
  verdict         TEXT NOT NULL CHECK (verdict IN ('核心','相关','外围','排除')),
  matched_rule_id INTEGER REFERENCES source_tier_rules(id) ON DELETE SET NULL,
  signals_json    TEXT,
  reason          TEXT,
  relevance_source TEXT,          -- local | model（相关性是本地算的还是模型判的）
  guard_failed    INTEGER NOT NULL DEFAULT 0,
  model           TEXT,
  prompt_version  TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grades_source ON source_grades(source_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_grades_verdict ON source_grades(verdict);
CREATE INDEX IF NOT EXISTS idx_grades_topic ON source_grades(topic, created_at DESC);

-- ---------------------------------------------------------------------------
-- 分层规则种子（INSERT OR IGNORE：靠 UNIQUE(match_type, pattern) 保证幂等）
-- 匹配优先级：url_regex > domain > domain_suffix > source_type（见 grading/tiers.py）
-- tier 语义：1 一手权威 / 2 学术索引与同行评议 / 3 社区与教程 / 4 未知或低质
-- ---------------------------------------------------------------------------

INSERT OR IGNORE INTO source_tier_rules (match_type, pattern, tier, weight, note) VALUES
  ('url_regex', '^https?://docs\.',                 1, 1.0, 'docs 开头的域名通常是官方文档'),
  ('url_regex', '^https?://[^/]+\.github\.io/',     3, 0.8, 'GitHub Pages 项目页'),

  ('domain', 'arxiv.org',              1, 1.0, '预印本源站（一手，未经同行评议）'),
  ('domain', 'openreview.net',         1, 1.0, '开放同行评议平台'),
  ('domain', 'docs.python.org',        1, 1.0, 'Python 官方文档'),
  ('domain', 'developer.mozilla.org',  1, 1.0, 'MDN 官方文档'),
  ('domain', 'pytorch.org',            1, 1.0, '框架官方文档'),
  ('domain', 'huggingface.co',         1, 0.9, '模型与数据集官方页'),
  ('domain', 'openai.com',             1, 0.9, '厂商官方'),
  ('domain', 'anthropic.com',          1, 0.9, '厂商官方'),
  ('domain', 'deepseek.com',           1, 0.9, '厂商官方'),
  ('domain', 'dl.acm.org',             2, 1.0, 'ACM 数字图书馆（同行评议）'),
  ('domain', 'ieeexplore.ieee.org',    2, 1.0, 'IEEE Xplore'),
  ('domain', 'link.springer.com',      2, 1.0, 'Springer'),
  ('domain', 'nature.com',             2, 1.0, 'Nature'),
  ('domain', 'science.org',            2, 1.0, 'Science'),
  ('domain', 'aclanthology.org',       2, 1.0, 'ACL Anthology'),
  ('domain', 'openalex.org',           2, 1.0, '学术索引'),
  ('domain', 'semanticscholar.org',    2, 1.0, '学术索引'),
  ('domain', 'github.com',             3, 1.0, '代码仓库（社区维护，质量需自判）'),
  ('domain', 'stackoverflow.com',      3, 1.0, '技术问答社区'),
  ('domain', 'w3schools.com',          3, 1.0, '教程站'),
  ('domain', 'zhihu.com',              3, 0.8, '中文问答社区'),
  ('domain', 'juejin.cn',              3, 0.7, '中文技术社区'),
  ('domain', 'medium.com',             3, 0.7, '个人博客平台'),
  ('domain', 'substack.com',           3, 0.7, '个人通讯'),
  ('domain', 'csdn.net',               4, 1.0, '内容质量参差，需人工复核'),
  ('domain', 'cnblogs.com',            4, 0.8, '博客平台，质量参差'),
  ('domain', 'baidu.com',              4, 1.0, '聚合与低质内容居多'),
  ('domain', 'sohu.com',               4, 1.0, '转载聚合居多'),

  ('domain_suffix', '.edu',     2, 1.0, '高校站点'),
  ('domain_suffix', '.edu.cn',  2, 1.0, '高校站点'),
  ('domain_suffix', '.ac.cn',   2, 1.0, '科研机构站点'),
  ('domain_suffix', '.gov',     2, 0.8, '政府站点'),
  ('domain_suffix', '.org',     3, 0.5, '机构站点，权重降低（.org 门槛很低）'),

  ('source_type', 'arxiv',             1, 1.0, '来自 arXiv API 的预印本'),
  ('source_type', 'official',          1, 1.0, '官方文档类来源'),
  ('source_type', 'openalex',          2, 1.0, 'OpenAlex 索引'),
  ('source_type', 'semantic_scholar',  2, 1.0, 'Semantic Scholar 索引'),
  ('source_type', 'github',            3, 1.0, 'GitHub 仓库'),
  ('source_type', 'web',               4, 1.0, '未分类网页，默认最低层');

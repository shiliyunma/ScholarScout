-- 跨轮研究台账（A1）：每个会话一行，存"目标 / 子问题 / 已用查询 / 已收来源 / 连续无新增次数"。
--
-- 为什么必须落库而不是留在上下文里：
--   ① `_fit_history` 会裁剪历史（且裁掉的正是早期证据），计划留在消息里迟早被裁掉
--      —— Anthropic 的多智能体研究系统把计划单独存 Memory，理由就是"上下文超 20 万 token 会被截断"；
--   ② 第 2 轮要"接着干"，就得知道"已经搜过什么、还缺什么"，否则只能换词重搜；
--   ③ 用它替代重发历史，还能让消息前缀更稳定（A6），拿到服务端缓存折扣。
--
-- 口径：`questions` 里每条带 status（open/covered）与 evidence（支撑它的 source_id）；
-- 覆盖与否由模型在回复末尾用状态标记回填（解析不出来就保持 open —— **不猜**）。
CREATE TABLE IF NOT EXISTS research_states (
  session_id   TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

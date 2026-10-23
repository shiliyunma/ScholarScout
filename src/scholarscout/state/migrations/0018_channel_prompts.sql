-- 每群/每会话绑模块（docs/21 §5.3；接口见 docs/01 §3.15，仓储见 state/channel_modules.py）。
--
-- 背景（这是用户指出的一个**假功能**）：文档说群可以"绑模块"（每个群用不同的模块/工具集），
-- 但 `state/migrations/` 止于 0017、`rpc/method_names()` 里没有任何 `channel.*module*` ——
-- 也就是根本绑不了。本迁移补数据侧，0018 之后 `channel.set_module` / `get_module` 才有地方落。
--
-- 为什么表名叫 `channel_modules` 而不是设计稿的 `channel_prompts`：
--   设计稿（docs/21 §5.3，2026-10-15）写的是"每群注入不同的提示词"，后来的口径改成
--   **模块只作为 `chat.send` 的 `toolsets` 运行时参数、不注入系统提示**（同一节 §5.3 与 §8
--   都写明了"不动提示词哈希"）。表里存的是"模块名"，不是提示词，所以名字叫 `channel_modules`
--   才不自相矛盾（迁移文件名沿用设计稿的 `0018_channel_prompts` 以便与文档对得上）。
--
-- 为什么不存 title / created_at（与设计稿的差异，理由如实写）：
--   `title` 只是"会话名，仅展示"，而它必须由**第一次收到该群消息**时自动登记；
--   本轮的落地范围是"绑定 + 解析顺序"（设置页那张表格是后续步骤），没有任何代码写它。
--   先建一个**没人写**的列，等于文档里有、库里永远是 NULL —— 那是又一个"假功能"。
--   要展示会话名时，权威来源是 `sessions.title`（渠道入站建的会话本来就有），不必抄一份。
--
-- 为什么主键是 (channel, chat_id) 而不是加一列自增 id：
--   一个会话只该有一个模块（换绑 = 覆盖，不是新增历史）。复合主键顺便让 upsert 的
--   `ON CONFLICT(channel, chat_id)` 有唯一索引可用，不需要再建索引。
--
-- 不进 DOMAIN_TABLES：那十张是**研究数据**（sources/documents/...），健康计数据此统计；
-- 本表是**入口层状态**（与 feishu_pairing_* / state_meta 同类），不占研究数据的分母。
--
-- 合法取值由代码层校验（`rpc/channel.py:MODULES`）：SQLite 里无法表达"取值必须属于
-- `runtime/registry.TOOLSETS` 这个**代码里的集合**"，写死 CHECK 会与之漂移
-- （0016 的 `user_facts.target` 是同一个取舍）。

CREATE TABLE IF NOT EXISTS channel_modules (
  channel    TEXT NOT NULL,   -- 渠道/平台标识（当前只有 'feishu'，与 channel.* 的 platform 同源）
  chat_id    TEXT NOT NULL,   -- 平台侧会话标识（飞书 chat_id：oc_...）；群与私聊都用它
  module     TEXT NOT NULL,   -- 模块名（research | kb | default；default = 跟全局默认）
  updated_at TEXT NOT NULL,   -- ISO-8601 带时区、带微秒（state/migrate.now_iso）
  PRIMARY KEY (channel, chat_id)
);

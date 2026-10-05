-- 0004：记录"为什么降级/失败"
--
-- 背景：extract_status 只说结果（ok / no_text_layer / mineru_failed），说不出**原因**。
-- 真机上 MinerU 免 token 的 Agent 服务会间歇性返回
-- `model service is temporarily unavailable`（state=failed）——
-- 这种信息不落库的话，"扫描版走 MinerU"在论文里就成了无法审计的黑盒：
-- 分不清"这份文档确实没有文字层"和"服务端抽风"。

ALTER TABLE documents ADD COLUMN extract_detail TEXT;

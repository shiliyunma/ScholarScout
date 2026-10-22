"""``source.fetch`` 的扫描版兜底**策略**：本地抽不出文字时交给 MinerU（决议第 8 题）。

**为什么与 ``fetch/mineru.py`` 分成两个文件**：那边是**接口客户端**（提交/轮询/取 Markdown，
只管"怎么调"），这里是**策略**（要不要调、先直传还是先提交 URL、两个都失败时怎么如实记）。
两边各有自己的测试：``tests/fetch/test_mineru.py`` 与 ``tests/tools/test_mineru_fallback_order.py``。

**为什么从 ``source_fetch.py`` 搬出来**：它曾埋在 600 行的工具模块中间，顺序策略一改就得翻整个
handler；而它的两条纪律（直传优先、失败原因不许吞）与抓取/入库/摘要无关。搬出来后
``source_fetch.py`` 只剩编排，这里只留"降级顺序"一件事。

``source_fetch.py`` 重导出 ``_mineru_fallback``，既有的
``from scholarscout.tools.source_fetch import _mineru_fallback`` 一字不改。
"""

from __future__ import annotations

from scholarscout.runtime.toolctx import ToolContext


def _mineru_fallback(
    ctx: ToolContext, url: str, *, content: bytes | None = None, filename: str = "scan.pdf"
) -> tuple[str, str, str, str]:
    """落地 MinerU 兜底（决议第 8 题）：本地抽不出文字时，交给官方 API。

    **顺序**：手里已经有字节就**优先直传**（预签名上传），失败再退回"提交 URL 让 MinerU 自己去下"。
    为什么这么排：URL 那条要求 MinerU 能下到那个 PDF（内网地址、需要登录、CDN 抖动、
    robots 拒爬都会失败），而我们**刚刚才把字节下载到本地** —— 直传少一个失败点，
    而且免 Token 也能用（Agent 轻量接口 ≤10MB/≤20 页）。实测 2026-10-07：本地造的
    无文字层扫描版样本直传 **10.3 秒**拿到中文 Markdown。

    失败要**如实**记 ``mineru_failed`` + 原因 —— 服务暂不可用与"文档确实没有内容"是两件事。
    """
    from scholarscout.fetch.mineru import parse_pdf_bytes, parse_pdf_url
    from scholarscout.fetch.settings import mineru_token

    http = ctx.require_http()
    token = mineru_token()
    # MinerU 轮询阶段的预算（分阶段超时里的"解析服务"那一段）：以前写死 180s，
    # 而"直传 + URL 提交"是**两次**调用 → 最坏 360s。改成配置项（默认 90s 一次）。
    poll_timeout_s = float(ctx.config.fetch_cfg.mineru_poll_timeout_s)
    direct_detail = ""

    if content:
        direct = parse_pdf_bytes(
            http,
            content,
            filename=filename,
            token=token,
            timeout_s=poll_timeout_s,
            limits=ctx.config.mineru,
        )
        if direct.ok:
            return direct.text, "ok", "mineru", f"{direct.engine} 解析（本地直传）"
        direct_detail = f"直传 {direct.state}: {direct.detail}"

    if not url:
        return "", "mineru_failed", "mineru", direct_detail or "既没有本地字节也没有 URL"

    result = parse_pdf_url(http, url, token=token, timeout_s=poll_timeout_s)
    if result.ok:
        return result.text, "ok", "mineru", f"{result.engine} 解析（URL 提交）"
    url_detail = f"URL 提交 {result.state}: {result.detail}"
    detail = f"{direct_detail}；{url_detail}" if direct_detail else url_detail
    return "", "mineru_failed", "mineru", detail


__all__ = ["_mineru_fallback"]

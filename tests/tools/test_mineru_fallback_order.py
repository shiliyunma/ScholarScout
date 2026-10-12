"""`source.fetch` 的扫描版兜底顺序：**本地字节直传优先**，失败才退回 URL 提交。

为什么顺序值得测：这条路是"扫描版能不能用"的唯一入口。老实现只会让 MinerU 自己来下那份 PDF，
于是内网地址、需要登录、CDN 抖动都会变成"抽不出正文"；而我们手里明明已经有字节。
"""

from __future__ import annotations

import pytest

from scholarscout.fetch.mineru import MineruText


def _ctx(http: object, poll_timeout_s: float = 90.0) -> object:
    """最小 ToolContext 替身：只要 ``require_http`` 与 ``config.fetch_cfg``。

    ``config`` 不是可省的 —— 真实的 :class:`~scholarscout.runtime.toolctx.ToolContext`
    一定有它（字段 ``config: Config``），而 MinerU 轮询预算
    （``mineru_poll_timeout_s``）就从这里读。
    """

    class FetchCfg:
        mineru_poll_timeout_s = poll_timeout_s

    class Config:
        fetch_cfg = FetchCfg()

    class Ctx:
        config = Config()

        def require_http(self) -> object:
            return http

    return Ctx()


def test_direct_upload_wins_and_url_is_not_tried(monkeypatch: pytest.MonkeyPatch) -> None:
    import scholarscout.fetch.mineru as mineru
    from scholarscout.tools.source_fetch import _mineru_fallback

    calls: list[str] = []

    def fake_bytes(http: object, content: bytes, **kwargs: object) -> MineruText:
        calls.append("bytes")
        assert content == b"PDF", "要把手里的字节原样传上去"
        assert kwargs["filename"] == "src_x.pdf"
        return MineruText("直传抽到的正文", "done", "agent", "")

    def fake_url(*args: object, **kwargs: object) -> MineruText:
        calls.append("url")
        return MineruText("", "failed", "agent", "不该走到这里")

    monkeypatch.setattr(mineru, "parse_pdf_bytes", fake_bytes)
    monkeypatch.setattr(mineru, "parse_pdf_url", fake_url)

    text, status, engine, detail = _mineru_fallback(
        _ctx(object()), "https://example.com/a.pdf", content=b"PDF", filename="src_x.pdf"
    )  # type: ignore[arg-type]
    assert (text, status, engine) == ("直传抽到的正文", "ok", "mineru")
    assert "本地直传" in detail
    assert calls == ["bytes"], "直传成功就不该再提交 URL"


def test_falls_back_to_url_when_upload_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import scholarscout.fetch.mineru as mineru
    from scholarscout.tools.source_fetch import _mineru_fallback

    calls: list[str] = []

    def fake_bytes(*args: object, **kwargs: object) -> MineruText:
        calls.append("bytes")
        return MineruText("", "failed", "agent", "403 SignatureDoesNotMatch")

    def fake_url(*args: object, **kwargs: object) -> MineruText:
        calls.append("url")
        return MineruText("URL 那边抽到了", "done", "agent", "")

    monkeypatch.setattr(mineru, "parse_pdf_bytes", fake_bytes)
    monkeypatch.setattr(mineru, "parse_pdf_url", fake_url)

    text, status, _, detail = _mineru_fallback(
        _ctx(object()), "https://example.com/a.pdf", content=b"PDF"
    )  # type: ignore[arg-type]
    assert (text, status) == ("URL 那边抽到了", "ok")
    assert "URL 提交" in detail
    assert calls == ["bytes", "url"]


def test_both_failures_are_reported_truthfully(monkeypatch: pytest.MonkeyPatch) -> None:
    """两条都失败时，**两个原因都要留下来** —— 否则排障时只知道"没成功"。"""
    import scholarscout.fetch.mineru as mineru
    from scholarscout.tools.source_fetch import _mineru_fallback

    monkeypatch.setattr(
        mineru, "parse_pdf_bytes", lambda *a, **k: MineruText("", "failed", "agent", "直传原因")
    )
    monkeypatch.setattr(
        mineru, "parse_pdf_url", lambda *a, **k: MineruText("", "timeout", "agent", "URL 原因")
    )

    text, status, engine, detail = _mineru_fallback(
        _ctx(object()), "https://example.com/a.pdf", content=b"PDF"
    )  # type: ignore[arg-type]
    assert text == "" and status == "mineru_failed" and engine == "mineru"
    assert "直传原因" in detail and "URL 原因" in detail


def test_no_bytes_goes_straight_to_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有本地字节（例如只拿到 URL）时，行为与老实现一致：直接提交 URL。"""
    import scholarscout.fetch.mineru as mineru
    from scholarscout.tools.source_fetch import _mineru_fallback

    calls: list[str] = []
    monkeypatch.setattr(
        mineru,
        "parse_pdf_bytes",
        lambda *a, **k: calls.append("bytes") or MineruText("", "failed", "agent", "不该调用"),
    )
    monkeypatch.setattr(
        mineru,
        "parse_pdf_url",
        lambda *a, **k: calls.append("url") or MineruText("URL 正文", "done", "agent", ""),
    )

    text, status, _, _ = _mineru_fallback(_ctx(object()), "https://example.com/a.pdf")  # type: ignore[arg-type]
    assert (text, status) == ("URL 正文", "ok")
    assert calls == ["url"]

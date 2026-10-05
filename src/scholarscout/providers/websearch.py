"""网页检索来源的共用件（bing / duckduckgo / searxng / tavily / serper / brave 共用）。

只有一件事在这里：把各家五花八门的返回**压成同一个 SourceCard**，
并把"明显的非内容结果"挡在外面 —— 检索结果的信噪比直接决定判权层的工作量。
"""

from __future__ import annotations

import base64
import binascii
import re
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

from scholarscout.providers.base import SourceCard

#: 网页来源统一用这个 source_type（判权规则里对应 tier 4 的兜底层）
WEB = "web"

#: 明显不是"资料"的结果：词典、翻译、百科、导航、文档站、商品页（按域名/路径子串匹配）
NOT_CONTENT_SUFFIXES: tuple[str, ...] = (
    "iciba.com",
    "dict.youdao.com",
    "fanyi.baidu.com",
    "translate.google.com",
    "dictionary.",  # dictionary.cambridge.org 之类
    "bing.com",  # 含 global.bing.com/dict 这类站内结果
    "duckduckgo.com",
    "baike.baidu.com",
    "zhidao.baidu.com",
    "wenda.so.com",
    "youtube.com",
    "bilibili.com/video",
    "csdn.net/download",
    "docin.com",
    "book118.com",
    "taobao.com",
    "jd.com",
)

_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
#: 结果里的转义残留（&nbsp; 等）在去标签后仍可能残留实体
_ENTITY = re.compile(r"&[a-zA-Z#0-9]{2,8};")
#: 去标签会在标签处留空格：``<b>agents</b>.`` → ``agents .`` —— 标点前不该有空格
_SPACE_BEFORE_PUNCT = re.compile(
    r"\s+([,.;:!?%)\]}\u3001\u3002\uff0c\uff1b\uff1a\uff01\uff1f\uff09\u3011\u300d\u300f])"
)
_SPACE_AFTER_OPEN = re.compile(r"([(\[{\uff08\u3010\u300c\u300e])\s+")

#: Bing 的跳转壳：``/ck/a?...&u=a1<base64url>``
_BING_REDIRECT_U = re.compile(r"[?&]u=a1([A-Za-z0-9_\-]+)")


def text_of(html_fragment: str) -> str:
    """去标签与实体，压成一行纯文本（用于标题与摘要）。"""
    if not html_fragment:
        return ""
    cleaned = _SCRIPT_STYLE.sub(" ", html_fragment)
    cleaned = _TAGS.sub(" ", cleaned)
    cleaned = unescape(cleaned)
    cleaned = _ENTITY.sub(" ", cleaned)
    # 去标签留下的空格要贴着标点收掉：``<b>agents</b>.`` → ``agents.``
    cleaned = _SPACE_BEFORE_PUNCT.sub(r"\1", cleaned)
    cleaned = _SPACE_AFTER_OPEN.sub(r"\1", cleaned)
    return _WS.sub(" ", cleaned).strip()


def snippet_of(html_fragment: str, *, limit: int = 300) -> str:
    text = text_of(html_fragment)
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def unwrap_redirect(url: str) -> str:
    """解开检索站点的跳转壳，拿到真实 URL。

    - Bing：``https://www.bing.com/ck/a?...&u=a1<base64url>``
    - DuckDuckGo：``https://duckduckgo.com/l/?uddg=<percent-encoded>``
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = f"https:{raw}"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()

    if "bing.com" in host and parsed.path.startswith("/ck/"):
        match = _BING_REDIRECT_U.search(raw)
        if match is not None:
            token = match.group(1)
            padded = token + "=" * (-len(token) % 4)
            try:
                decoded = base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
            except (binascii.Error, ValueError):
                return ""
            if decoded.startswith("http"):
                return decoded
        return ""

    if "duckduckgo.com" in host:
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return raw


def parse_year(text: str | None) -> int | None:
    """从标题/摘要里捞一个 4 位数年份（捞不到就 None，不猜）。"""
    if not text:
        return None
    match = _YEAR.search(text)
    return int(match.group(0)) if match is not None else None


def is_content_url(url: str) -> bool:
    """是不是一条像样的"资料"链接（挡掉词典/跳转壳/非 http 的结果）。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    target = f"{parsed.hostname.lower()}{parsed.path.lower()}"
    return not any(target.endswith(suffix) or suffix in target for suffix in NOT_CONTENT_SUFFIXES)


def card_from(
    *,
    url: str,
    title: str,
    snippet: str | None = None,
    source_type: str = WEB,
    venue: str | None = None,
    extra: dict[str, str] | None = None,
) -> SourceCard | None:
    """把一条网页结果压成 SourceCard；不合格（无标题/非内容链接）返回 None。"""
    real_url = unwrap_redirect(url)
    if not real_url or not is_content_url(real_url):
        return None
    clean_title = text_of(title)
    if not clean_title:
        return None
    clean_snippet = snippet_of(snippet or "") or None
    return SourceCard(
        url=real_url,
        title=clean_title,
        source_type=source_type,
        snippet=clean_snippet,
        year=parse_year(clean_title) or parse_year(clean_snippet),
        venue=venue,
        extra=extra or {},
    )


def cards_from_pairs(
    items: list[tuple[str, str, str | None]], *, source_type: str = WEB
) -> list[SourceCard]:
    """批量构造，顺带丢掉不合格项。"""
    out: list[SourceCard] = []
    for url, title, snippet in items:
        card = card_from(url=url, title=title, snippet=snippet, source_type=source_type)
        if card is not None:
            out.append(card)
    return out

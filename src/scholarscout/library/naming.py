"""命名要的元数据：**类型 / 发表年月 / 收录年月 / 真标题 / 会话目录名** —— 全从库与 provider 元数据推，不编（docs/19 §1/§2）。

三条口径（都对应一个曾经会犯的错法）：

1. **年-月优先取 arXiv 号自带的 YYMM**（``2410.24164`` → ``2024-10``）—— 它是资料自己的时间戳，
   比「什么时候被我们抓到」准得多。
2. **取不到月份落 ``unknown``**，绝不用抓取日期冒充发表日期（与 docs/05「未知年份不编」同口径）。
   反过来，**收录年月**（``collected_month``）取的就是 ``documents.fetched_at``，取不到同样落 ``unknown``。
3. **标题取不到就回落到域名**，绝不用 URL 或抓取时间编一个看着像真标题的字符串。

会话目录名也在这里定：标题 slug；**两个会话标题同名时追加短会话 id** 区分（docs/19 §2）。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from scholarscout import paths
from scholarscout.state.db import Database

#: arXiv 新式编号 ``YYMM.NNNNN``（月份必须 01–12，所以 ``9946.2026`` 这类 DOI 片段不会被误判）。
_ARXIV_ID_RE = re.compile(r"(?<!\d)(\d{2})(0[1-9]|1[0-2])\.\d{4,5}(?!\d)")

#: meta_json 里可能出现的\"发表\"日期字段（优先）。
_PUBLISHED_KEYS: tuple[str, ...] = ("published", "published_at", "publication_date", "date")

#: \"更新\"日期字段（次之）。
_UPDATED_KEYS: tuple[str, ...] = (
    "pushed_at",
    "updated",
    "updated_at",
    "last_modified",
    "modified",
    "created_at",
)

#: 只认 ``YYYY-MM``（月份合法），``YYYY-MM-DD`` 也能命中前两段。
_MONTH_RE = re.compile(r"(\d{4})-(0[1-9]|1[0-2])")

#: 看着像 URL 的\"标题\"（``sources.title`` 缺真标题时会回落成 url）—— 不能当标题用。
_URL_RE = re.compile(r"^\s*(?:https?://|www\.)", re.IGNORECASE)


def digits_to_year_month(raw: str) -> str | None:
    """把 arXiv 的 ``YYMM.NNNNN`` 谱式转成 ``YYYY-MM``；不是这个形状就回 ``None``。"""
    match = _ARXIV_ID_RE.search(raw)
    if match is None:
        return None
    yy = int(match.group(1))
    month = int(match.group(2))
    # arXiv 的两位年份：1990 年前后是 19YY，之后按 20YY（本项目数据全在 20YY，这条兜底而已）
    year = 1900 + yy if yy >= 90 else 2000 + yy
    return f"{year:04d}-{month:02d}"


def year_month_from_arxiv(url: str | None, meta: Mapping[str, Any]) -> str | None:
    """从 arXiv 号推年-月。先看 ``external_id``（最干净），再看 url 与 pdf_url（需 arxiv 域名）。"""
    external = str(meta.get("external_id") or "")
    if _ARXIV_ID_RE.fullmatch(external.strip()):
        return digits_to_year_month(external)
    for candidate in (url, meta.get("pdf_url")):
        text = str(candidate or "")
        if "arxiv.org" in text.lower():
            found = digits_to_year_month(text)
            if found:
                return found
    return None


def year_month_from_meta(meta: Mapping[str, Any]) -> str | None:
    """从元数据的日期字段推年-月：发表优先，更新次之；认不出来回 ``None``（不猜）。"""
    for keys in (_PUBLISHED_KEYS, _UPDATED_KEYS):
        for key in keys:
            match = _MONTH_RE.search(str(meta.get(key) or ""))
            if match is not None:
                return f"{match.group(1)}-{match.group(2)}"
    return None


def year_month_for(url: str | None, meta: Mapping[str, Any] | None = None) -> str:
    """资料自己的\"发表/更新\"月份（``YYYY-MM``）；推不出来回 ``unknown``。"""
    table = meta or {}
    return year_month_from_arxiv(url, table) or year_month_from_meta(table) or "unknown"


def pick_title(*candidates: str | None) -> str:
    """挑一个**真标题**：跳过空值与 URL 形状的字符串（那些是回落值，不是标题）。"""
    for candidate in candidates:
        text = (candidate or "").strip()
        if text and not _URL_RE.match(text):
            return text
    return ""


def domain_of(url: str | None) -> str:
    """URL → 主机名（小写、去 ``www.``）；解析不出来回空串（由调用方回落到 ``untitled``）。"""
    from urllib.parse import urlparse

    host = (urlparse(str(url or "")).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


#: 会话目录名未知时用的段值（与 ``paths.UNKNOWN_SESSION`` 同源）。
_UNKNOWN_SESSION = "unknown"


def collected_month(fetched_at: str | None) -> str:
    """**收录年月**：``documents.fetched_at``（ISO 时间戳）→ ``YYYY-MM``；取不到回 ``unknown``。

    与发表年月的区别（用户 2026-10-11 明确的两条口径）：收录年月是"我什么时候采到它的"，
    发表年月是"它自己什么时候出的"。两个都进索引（``docs/19`` §2），缺哪个写哪个的 ``unknown``。
    """
    match = _MONTH_RE.search(str(fetched_at or ""))
    if match is None:
        return _UNKNOWN_SESSION
    return f"{match.group(1)}-{match.group(2)}"


def session_short_id(session_id: str) -> str:
    """会话的**短 id**（同名会话用来区分目录）：取 id 里的字母数字，末尾 4 位。

    ``ses_s2d2ytta`` → ``ytta``。末尾 4 位是最没有规律的一段，短且够用。
    """
    alnum = re.sub(r"[^0-9A-Za-z]", "", str(session_id or ""))
    return alnum[-4:] if len(alnum) >= 4 else (alnum or "ses0")


def session_dir_name(title: str | None, session_id: str, *, colliding: bool = False) -> str:
    """会话目录名：标题 slug；``colliding=True``（有别的会话同名）时追加 ``--<短会话 id>``。

    标题取不到就回落成 ``unknown``（**不编一个看着像会话名的字符串**）。
    """
    slug = paths.slugify(title or "", fallback=_UNKNOWN_SESSION)
    if colliding:
        return f"{slug}--{session_short_id(session_id)}"
    return slug


def session_dir_names(rows: Iterable[tuple[str, str | None]]) -> dict[str, str]:
    """``(session_id, title)`` 列表 → ``{session_id: 目录名}``；**同名的都追加短 id**。

    为什么不是"第一个用裸名、后面的加后缀"：那样结果依赖遍历顺序，换个入口就算出不同的目录。
    **「slug 撞车 → 撞车的每个都带短 id」与顺序无关**，重算多少次都一样（迁移脚本要幂等）。
    """
    items = [(str(session_id), title) for session_id, title in rows]
    slugs = [paths.slugify(title or "", fallback=_UNKNOWN_SESSION) for _, title in items]
    counts = Counter(slugs)
    return {
        session_id: session_dir_name(title, session_id, colliding=counts[slug] > 1)
        for (session_id, title), slug in zip(items, slugs, strict=True)
    }


def session_dir_map(db: Database) -> dict[str, str]:
    """全库会话 → 目录名（同名加短 id）。``source.fetch`` 与迁移脚本共用同一份口径。"""
    return session_dir_names(
        (str(row["id"]), row["title"]) for row in db.query("SELECT id, title FROM sessions")
    )


def session_dir_for(db: Database, session_id: str | None) -> str:
    """这个会话在资料布局里的目录名；没有会话/查不到就落 ``unknown``（不编）。"""
    if not session_id:
        return _UNKNOWN_SESSION
    return session_dir_map(db).get(str(session_id), _UNKNOWN_SESSION)

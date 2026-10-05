"""摘要流水线（docs/04 §4 的七步）：

    取正文 → 清洗 → 截断+拼提示词 → 调模型 → 解析 → 守卫 → 落库（落库由 state/ingest 做）

关键约束：
- 一次模型调用，**不做分块摘要**（决议 5；留给以后的 RAG）
- 长度不合规用确定性裁剪，不发起第二次"修复"调用（照 AIHOT）
- 模型失败不抛给上层崩掉：退化成"用元数据出的摘要"并标 ``degraded=1``
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from scholarscout.providers.llm import ChatReply, LlmClient, LlmError
from scholarscout.summary import guards
from scholarscout.summary.prompts import PromptPack
from scholarscout.summary.settings import SummarySettings

#: source_type → 提示词体裁
KIND_BY_SOURCE_TYPE = {
    "arxiv": "paper",
    "openalex": "paper",
    "semantic_scholar": "paper",
    "github": "repo",
    "web": "doc",
    "official": "doc",
}
#: 体裁 → prompts/ 下的文件名（**显式映射**，别让 DB 里的 kind 与文件名偷偷耦合）
PROMPT_BY_KIND = {
    "paper": "summarize-paper",
    "doc": "summarize-doc",
    "repo": "summarize-repo",
    "scanned": "summarize-doc",  # 扫描件没有正文，用文档体裁的模板
    "degraded": "summarize-doc",
}
#: 这些抽取状态说明"拿不到正文"，降级成 scanned
NO_TEXT_STATUS = {"no_text_layer", "mineru_failed", "empty", "blocked", "too_large"}
#: 正文少于这个字符数就改用摘要字段（多半是只抓到了导航栏）
MIN_BODY_CHARS = 200

_TITLE_LINE = re.compile(r"^\s*title_zh\s*[:：]\s*(.+)$", re.MULTILINE)
_SUMMARY_LINE = re.compile(r"^\s*summary_short\s*[:：]\s*(.+)$", re.MULTILINE)
_POINTS_HEAD = re.compile(r"^\s*points\s*[:：]\s*$", re.MULTILINE)


@dataclass(frozen=True)
class SummaryInput:
    """一份待摘要的文档（正文可以是空字符串）。"""

    title: str
    body: str = ""
    source_type: str = "web"
    source_name: str = ""
    abstract: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    venue: str | None = None
    extract_status: str = "ok"
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SummaryDraft:
    kind: str
    title_zh: str
    summary_short: str
    points: tuple[str, ...]
    why_relevant: str | None
    model: str
    prompt_version: str
    input_chars: int
    input_truncated: bool
    degraded: bool
    guard_failed: bool
    ungrounded: tuple[str, ...] = ()
    error: str | None = None

    def points_json(self) -> str | None:
        import json

        return json.dumps(list(self.points), ensure_ascii=False) if self.points else None


@dataclass(frozen=True)
class ParsedSummary:
    title_zh: str
    summary_short: str
    points: tuple[str, ...]
    labelled: bool


def choose_kind(source_type: str, extract_status: str = "ok") -> str:
    if extract_status in NO_TEXT_STATUS:
        return "scanned"
    return KIND_BY_SOURCE_TYPE.get(source_type, "doc")


def prompt_name_for(kind: str) -> str:
    """体裁 → prompts/ 下的文件名（不带 .md）。未知体裁回落到文档模板。"""
    return PROMPT_BY_KIND.get(kind, "summarize-doc")


def variables_for(kind: str, item: SummaryInput) -> dict[str, str]:
    """给不同体裁的提示词准备变量（模板里用了哪个就必须给哪个，否则早失败）。"""
    common = {"title": item.title, "sourceName": item.source_name or "（未注明）"}
    if kind == "paper":
        return {
            **common,
            "publishedYear": str(item.year or "未注明"),
            "venue": item.venue or "未注明",
            "authors": "、".join(item.authors) if item.authors else "未注明",
            "body": "",
        }
    if kind == "repo":
        return {
            **common,
            "repoFullName": item.extra.get("repo_full_name", item.title),
            "stars": item.extra.get("stars", "未知"),
            "language": item.extra.get("language", "未知"),
            "license": item.extra.get("license", "未知"),
            "pushedAt": item.extra.get("pushed_at", "未知"),
            "latestRelease": item.extra.get("latest_release", "未知"),
            "body": "",
        }
    return {**common, "docKind": item.extra.get("doc_kind", "网页"), "body": ""}


def parse_output(text: str) -> ParsedSummary:
    """解析 ``title_zh:`` / ``summary_short:`` / ``points:`` 标签；丢标签时按行兜底。"""
    raw = (text or "").strip()
    title_match = _TITLE_LINE.search(raw)
    summary_match = _SUMMARY_LINE.search(raw)
    points: list[str] = []
    head = _POINTS_HEAD.search(raw)
    if head is not None:
        for line in raw[head.end() :].splitlines():
            stripped = line.strip()
            if stripped.startswith(("-", "*", "•")):
                points.append(stripped.lstrip("-*• ").strip())
            elif stripped and points:
                break

    title_zh = title_match.group(1).strip() if title_match else ""
    summary_short = summary_match.group(1).strip() if summary_match else ""
    labelled = bool(title_zh or summary_short)
    if not labelled:
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        title_zh = lines[0] if lines else ""
        summary_short = " ".join(lines[1:]) if len(lines) > 1 else ""
    if not title_zh and summary_short:
        title_zh = summary_short[:24]
    return ParsedSummary(
        title_zh=title_zh,
        summary_short=summary_short,
        points=tuple(points),
        labelled=labelled,
    )


def summarize(
    llm: LlmClient,
    item: SummaryInput,
    *,
    settings: SummarySettings,
    prompts: PromptPack | None = None,
) -> SummaryDraft:
    """跑一次摘要。**不抛异常**：模型失败降级成元数据摘要（degraded=1）。"""
    pack = prompts or PromptPack(settings.prompts_dir)
    kind = choose_kind(item.source_type, item.extract_status)
    prompt_name = prompt_name_for(kind)

    cleaned = guards.clean_text(item.body)
    if len(cleaned) < MIN_BODY_CHARS:
        cleaned = guards.clean_text(item.abstract) or cleaned
    input_truncated = len(cleaned) > settings.truncate_chars
    body = guards.clamp_text(cleaned, settings.truncate_chars)
    version = pack.version_of(prompt_name)

    if not body:
        return _degraded_draft(
            item,
            kind=kind,
            version=version,
            settings=settings,
            error="no_content",
            model="",
        )

    variables = variables_for(kind, item)
    variables["body"] = body
    prompt = pack.compose(prompt_name, **variables)
    try:
        reply: ChatReply = llm.chat([{"role": "user", "content": prompt}])
    except LlmError as exc:
        return _degraded_draft(
            item,
            kind=kind,
            version=version,
            settings=settings,
            error=f"{exc.kind}: {exc}",
            model="",
        )

    parsed = parse_output(reply.content)
    summary_short = parsed.summary_short
    guard_failed = False
    if not guards.length_ok(
        summary_short,
        source_chars=len(cleaned),
        min_chars=settings.summary_min_chars,
        max_chars=settings.summary_max_chars,
        max_sentences=settings.max_sentences,
    ):
        summary_short = guards.compact_summary(summary_short, max_chars=settings.summary_max_chars)
        guard_failed = not guards.length_ok(
            summary_short,
            source_chars=len(cleaned),
            min_chars=settings.summary_min_chars,
            max_chars=settings.summary_max_chars,
            max_sentences=settings.max_sentences,
        )

    ungrounded: tuple[str, ...] = ()
    if settings.guard_check_numbers:
        ungrounded = tuple(guards.ungrounded_numbers(summary_short, cleaned))
        if ungrounded:
            guard_failed = True

    return SummaryDraft(
        kind=kind,
        title_zh=parsed.title_zh or item.title,
        summary_short=summary_short,
        points=parsed.points[: settings.max_points],
        why_relevant=None,
        model=reply.model or "",
        prompt_version=version,
        input_chars=len(cleaned),
        input_truncated=input_truncated,
        degraded=False,
        guard_failed=guard_failed,
        ungrounded=ungrounded,
    )


def _degraded_draft(
    item: SummaryInput,
    *,
    kind: str,
    version: str,
    settings: SummarySettings,
    error: str,
    model: str,
) -> SummaryDraft:
    """没有正文／模型挂了：用**原文自己的**标题与摘要字段交付，并如实标记 degraded。"""
    fallback = guards.clean_text(item.abstract) or guards.clean_text(item.title)
    return SummaryDraft(
        kind="degraded" if not fallback else kind,
        title_zh=item.title,
        summary_short=guards.clamp_text(fallback, settings.summary_max_chars),
        points=(),
        why_relevant=None,
        model=model,
        prompt_version=version,
        input_chars=len(guards.clean_text(item.body)),
        input_truncated=False,
        degraded=True,
        guard_failed=False,
        error=error,
    )


def prompts_dir_of(settings: SummarySettings) -> Path:
    return settings.prompts_dir

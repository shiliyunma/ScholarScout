"""``source.fetch`` 契约：落盘、抽正文、摘要入库、去重、PDF 无文字层、路径可发现。

全程离线（MockTransport + 桩模型）；文件写到 tmp 的 data/ 下（SCHOLARSCOUT_HOME 重定向）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from scholarscout import paths
from scholarscout.fetch.http import HttpClient
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state.ingest import upsert_source
from scholarscout.state.search import search_summaries
from scholarscout.tools import register_tools
from scholarscout.tools.source_fetch import PARAMETERS, SPEC, guess_source_type, source_fetch

from ..conftest import StubLlm, reply

PAGE = (
    "<html lang='zh'><head><title>教育智能体文档</title></head><body><main>"
    + "<p>教育智能体先检索再作答，报告在 MATH 数据集上把准确率从 42% 提升到 58%。</p>" * 20
    + "</main></body></html>"
)
SUMMARY_TEXT = (
    "title_zh: 教育智能体文档\n"
    "summary_short: 该文档介绍先检索再作答的教育智能体做法，并把抓到的资料原件与全文分别落盘，"
    "正文不再进入数据库。示例报告在 MATH 数据集上把准确率从 42% 提升到 58%，"
    "并给出了可直接运行的命令。\n"
    "points:\n"
    "- 在 MATH 上准确率由 42% 提升到 58%\n"
)


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


def _redirect_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SCHOLARSCOUT_HOME", str(tmp_path))
    paths.ensure_dirs(tmp_path)


def _tool_ctx(
    ctx: AppContext,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    llm: object | None = None,
) -> ToolContext:
    ctx.http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    if llm is not None:
        ctx.llm = llm  # type: ignore[assignment]
    return ToolContext(db=ctx.db, config=ctx.config, http=ctx.http, llm=ctx.llm, session_id="ses_t")


def _html_handler(calls: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"})

    return handler


def test_spec_shape() -> None:
    assert SPEC.name == "source.fetch"
    assert PARAMETERS["additionalProperties"] is False
    assert set(PARAMETERS["properties"]) == {"url", "source_id", "force"}


def test_fetch_html_writes_files_and_summary(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_home(monkeypatch, tmp_path)
    calls: list[str] = []
    tool_ctx = _tool_ctx(ctx, _html_handler(calls), llm=StubLlm(reply(SUMMARY_TEXT)))

    result = source_fetch(tool_ctx, url="https://docs.example.com/guide")
    data = result["data"]

    assert data["cached"] is False
    assert data["extract_status"] == "ok"
    assert data["extract_engine"] == "local"
    assert data["chars"] > 500
    assert data["lang"] == "zh"

    raw_file = tmp_path / data["raw_path"]
    text_file = tmp_path / data["text_path"]
    assert raw_file.is_file(), "原件必须落盘"
    assert text_file.is_file(), "全文必须落盘（正文不入库）"
    assert "教育智能体先检索再作答" in text_file.read_text(encoding="utf-8")

    summary = data["summary"]
    assert summary is not None
    assert summary["title_zh"] == "教育智能体文档"
    assert summary["degraded"] is False
    assert summary["guard_failed"] is False
    assert result["refs"]["summary_ids"] == [summary["summary_id"]]
    # 正文不进库：库里只有元数据与摘要
    assert ctx.db.scalar_int("SELECT count(*) FROM documents") == 1
    assert ctx.db.scalar_int("SELECT count(*) FROM summaries") == 1
    assert search_summaries(ctx.db, "教育智能体"), "摘要必须可被 FTS 检索到"


def test_second_fetch_uses_dedupe_fast_path(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_home(monkeypatch, tmp_path)
    calls: list[str] = []
    llm = StubLlm(reply(SUMMARY_TEXT))
    tool_ctx = _tool_ctx(ctx, _html_handler(calls), llm=llm)

    first = source_fetch(tool_ctx, url="https://docs.example.com/guide")
    second = source_fetch(tool_ctx, url="https://docs.example.com/guide")

    assert second["data"]["cached"] is True
    assert second["data"]["doc_id"] == first["data"]["doc_id"]
    assert len(llm.calls) == 1, "重复抓取不应再调一次模型"
    assert ctx.db.scalar_int("SELECT count(*) FROM summaries") == 1


def test_force_bypasses_dedupe(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_home(monkeypatch, tmp_path)
    calls: list[str] = []
    llm = StubLlm(reply(SUMMARY_TEXT), reply(SUMMARY_TEXT))
    tool_ctx = _tool_ctx(ctx, _html_handler(calls), llm=llm)
    source_fetch(tool_ctx, url="https://docs.example.com/guide")
    again = source_fetch(tool_ctx, url="https://docs.example.com/guide", force=True)
    assert again["data"]["cached"] is False
    assert len(llm.calls) == 2, "force 应重跑摘要"
    assert ctx.db.scalar_int("SELECT count(*) FROM summaries") == 1, "同版本提示词仍是同一行"


def test_pdf_without_text_layer_is_reported_honestly(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_home(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"%PDF-1.4 not really a pdf", headers={"content-type": "application/pdf"}
        )

    tool_ctx = _tool_ctx(ctx, handler, llm=StubLlm(reply(SUMMARY_TEXT)))
    result = source_fetch(tool_ctx, url="https://example.com/paper.pdf")
    data = result["data"]
    assert data["extract_status"] == "no_text_layer"
    assert data["chars"] == 0
    assert (tmp_path / data["raw_path"]).is_file(), "抽不出正文也要留下原件"
    assert data["summary"] is not None, "拿不到正文仍要交付（degraded）摘要"
    assert data["summary"]["degraded"] is True


def test_fetch_by_source_id(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_home(monkeypatch, tmp_path)
    source_id, _ = upsert_source(
        ctx.db,
        {
            "url": "https://docs.example.com/guide",
            "url_canonical": "https://docs.example.com/guide",
            "domain": "docs.example.com",
            "title": "教育智能体文档",
            "source_type": "official",
        },
    )
    tool_ctx = _tool_ctx(ctx, _html_handler([]), llm=StubLlm(reply(SUMMARY_TEXT)))
    result = source_fetch(tool_ctx, source_id=source_id)
    assert result["data"]["source_id"] == source_id


def test_missing_target_raises(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_home(monkeypatch, tmp_path)
    tool_ctx = _tool_ctx(ctx, _html_handler([]))
    with pytest.raises(ValueError, match="必须给"):
        source_fetch(tool_ctx)


def test_unknown_source_id_raises(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_home(monkeypatch, tmp_path)
    tool_ctx = _tool_ctx(ctx, _html_handler([]))
    with pytest.raises(ValueError, match="未知 source_id"):
        source_fetch(tool_ctx, source_id="src_nope")


def test_without_llm_summary_is_marked_unavailable(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_home(monkeypatch, tmp_path)
    ctx.llm = None
    tool_ctx = _tool_ctx(ctx, _html_handler([]))
    result = source_fetch(tool_ctx, url="https://docs.example.com/guide")
    assert result["data"]["summary"] == {"error": "llm_not_configured", "degraded": True}
    assert result["refs"]["summary_ids"] == []


def test_archive_paths_reports_existence_after_fetch(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_home(monkeypatch, tmp_path)
    tool_ctx = _tool_ctx(ctx, _html_handler([]), llm=StubLlm(reply(SUMMARY_TEXT)))
    doc_id = source_fetch(tool_ctx, url="https://docs.example.com/guide")["data"]["doc_id"]

    payload = dispatch(ctx, "archive.paths", {"doc_id": doc_id})
    assert payload["exists"] == {"raw_path": True, "text_path": True}
    assert payload["raw_path"].startswith("data/raw/")


def test_guess_source_type() -> None:
    assert guess_source_type("https://arxiv.org/abs/1") == "arxiv"
    assert guess_source_type("https://github.com/a/b") == "github"
    assert guess_source_type("https://docs.example.com/x") == "official"
    assert guess_source_type("https://blog.example.com/x") == "web"


def test_source_fetch_is_visible_to_model_via_loop(ctx: AppContext) -> None:
    """工具注册后，loop 必须把它声明给模型（研究工具集）。"""
    from scholarscout.runtime import registry

    names = registry.registered_names()
    assert "research.search" in names
    assert "source.fetch" in names
    assert registry.get_tools(["research"], features=ctx.config.features)

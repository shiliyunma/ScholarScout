"""``source.fetch`` 契约：落盘、抽正文、摘要入库、去重、PDF 无文字层、路径可发现。

全程离线（MockTransport + 桩模型）；文件写到 tmp 的 data/ 下（SCHOLARSCOUT_HOME 重定向）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
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

from ..conftest import StubLlm, reply, tool_call

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


def _blank_pdf_bytes() -> bytes:
    """造一份**合法但没有文字层**的 PDF —— 扫描版的真实形态。

    （别拿 b"%PDF-1.4 not really a pdf" 糊弄：pymupdf 会抛 FileDataError，
    走到的是"打开失败"分支，测不到"没有文字层 → 交 MinerU"这条主路径。）
    """
    import pymupdf

    document = pymupdf.open()
    document.new_page()
    payload = document.tobytes()
    document.close()
    return payload


def test_pdf_without_text_layer_falls_back_to_mineru(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无文字层的 PDF（多为扫描版）→ 走 MinerU，按结果如实标注。

    这里把 MinerU 逼成"连不上"，验的是**诚实降级**：
    状态必须是 mineru_failed 且带原因，而不是含糊的 no_text_layer ——
    两者在论文里是不同的事（"文档没内容" vs "解析服务没干活"）。
    """
    _redirect_home(monkeypatch, tmp_path)
    pdf = _blank_pdf_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if "mineru.net" in str(request.url):
            raise httpx.ConnectError("mineru unreachable", request=request)
        return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})

    tool_ctx = _tool_ctx(ctx, handler, llm=StubLlm(reply(SUMMARY_TEXT)))
    result = source_fetch(tool_ctx, url="https://example.com/paper.pdf")
    data = result["data"]
    assert data["extract_status"] == "mineru_failed"
    assert data["extract_engine"] == "mineru"
    detail = data["extract_detail"] or ""
    assert "没有文字层" in detail, "要能看出本地是为什么交出去的"
    assert "network" in detail, "要能看出 MinerU 是为什么没成"
    assert data["chars"] == 0
    assert (tmp_path / data["raw_path"]).is_file(), "抽不出正文也要留下原件"
    assert data["summary"] is not None, "拿不到正文仍要交付（degraded）摘要"
    assert data["summary"]["degraded"] is True


def test_pdf_without_text_layer_and_mineru_disabled_stays_honest(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关掉 MinerU（features.enable_mineru=false）→ 停在 no_text_layer，不硬编也不假装成功。"""
    _redirect_home(monkeypatch, tmp_path)
    ctx.config = replace(ctx.config, features=replace(ctx.config.features, enable_mineru=False))
    pdf = _blank_pdf_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if "mineru.net" in str(request.url):  # pragma: no cover - 不该被调用
            raise AssertionError("关掉开关后不该访问 MinerU")
        return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})

    tool_ctx = _tool_ctx(ctx, handler, llm=StubLlm(reply(SUMMARY_TEXT)))
    data = source_fetch(tool_ctx, url="https://example.com/paper.pdf")["data"]
    assert data["extract_status"] == "no_text_layer"
    assert data["extract_engine"] == "local"
    assert "扫描版" in (data["extract_detail"] or "")


def test_mineru_success_path_uses_markdown(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MinerU 成功时正文来自 Markdown 链接，engine 记 mineru，detail 记走的是哪条接口。"""
    _redirect_home(monkeypatch, tmp_path)
    task_id = "task-123"
    pdf = _blank_pdf_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/v1/agent/parse/url"):
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"task_id": task_id}})
        if f"/api/v1/agent/parse/{task_id}" in url:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "task_id": task_id,
                        "state": "done",
                        "markdown_url": "https://cdn.example.com/full.md",
                    },
                },
            )
        if url.endswith("/full.md"):
            return httpx.Response(200, text="# 扫描版论文\n\n这是 MinerU 抽出来的正文。" * 5)
        return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})

    tool_ctx = _tool_ctx(ctx, handler, llm=StubLlm(reply(SUMMARY_TEXT)))
    data = source_fetch(tool_ctx, url="https://example.com/scanned.pdf")["data"]
    assert data["extract_status"] == "ok"
    assert data["extract_engine"] == "mineru"
    assert "agent 解析" in (data["extract_detail"] or "")
    assert data["chars"] > 0


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
    # 新布局：会话/格式 分桶 + 人类可读文件名，全文与原件同名只差后缀（docs/19 §2）。
    # 这里没有对应的会话行 → 会话段落 unknown（不编一个看着像会话名的目录）。
    assert payload["raw_path"].startswith("data/raw/unknown/doc/教育智能体文档--")
    assert payload["raw_path"].endswith(".html")
    assert payload["text_path"].startswith("data/text/unknown/doc/教育智能体文档--")
    assert payload["text_path"].endswith(".txt")


def test_fetch_uses_session_dir_from_layout(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """落盘目录的会话段取**当前会话的目录名**（标题 slug），格式段取 ``source_type`` 的映射。"""
    _redirect_home(monkeypatch, tmp_path)
    ctx.db.execute(
        "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
        ("ses_t", "教育智能体调研", "2026-10-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00"),
    )
    tool_ctx = _tool_ctx(ctx, _html_handler([]), llm=StubLlm(reply(SUMMARY_TEXT)))

    data = source_fetch(tool_ctx, url="https://docs.example.com/guide")["data"]

    assert data["raw_path"].startswith("data/raw/教育智能体调研/doc/")
    assert data["text_path"].startswith("data/text/教育智能体调研/doc/")
    assert (tmp_path / data["raw_path"]).is_file()


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
    assert registry.get_tools(["research"])


# ---------------------------------------------------------------------------
# 单源失败与分阶段超时（2026-10-12 补）：真机一轮 6 次 source.fetch 合计 836s，
# 界面判"调用失败"整链停摆。两条纪律写进测试：
#   ① 单条来源抓不到 → 工具自己回 ok=false + 原因，绝不冒泡成异常把整轮作废；
#   ② 抽取阶段有墙钟预算，超了如实标 extract_timeout，不给整轮挂死的机会。
# ---------------------------------------------------------------------------


def test_dead_source_returns_ok_false_with_reason_instead_of_raising(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连不上的来源：回 ``ok=false`` + 可读原因；同一轮里的好来源照旧完成。"""
    _redirect_home(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if "dead.example.com" in str(request.url):
            raise httpx.ConnectError("host unreachable", request=request)
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"})

    tool_ctx = _tool_ctx(ctx, handler, llm=StubLlm(reply(SUMMARY_TEXT)))

    bad = source_fetch(tool_ctx, url="https://dead.example.com/paper")
    assert bad["ok"] is False, "抓取失败必须是 ok=false（异常冒泡会让模型以为整轮废了）"
    assert bad["data"]["error_kind"] == "network"
    assert "抓取失败" in str(bad["error"]) and "dead.example.com" in str(bad["error"])
    assert bad["refs"] == {}, "失败条目不该报出任何 source_id"

    good = source_fetch(tool_ctx, url="https://docs.example.com/guide")
    assert good["ok"] is True and good["data"]["chars"] > 0
    assert ctx.db.scalar_int("SELECT count(*) FROM documents") == 1, "只有成功那条落库"


def test_timeout_source_is_classified_as_timeout_not_a_crash(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超时的来源按 timeout 分类回来（原因里带 URL），调用方据此跳过这一条。"""
    _redirect_home(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow", request=request)

    tool_ctx = _tool_ctx(ctx, handler, llm=StubLlm(reply(SUMMARY_TEXT)))
    result = source_fetch(tool_ctx, url="https://slow.example.com/paper")

    assert result["ok"] is False
    assert result["data"]["error_kind"] == "timeout"
    assert result["data"]["requested_url"] == "https://slow.example.com/paper"
    assert ctx.db.scalar_int("SELECT count(*) FROM documents") == 0


def _text_pdf_bytes(pages: int = 3) -> bytes:
    """造一份**每页都有文字层**的多页 PDF（用来验"抽到一半超预算"这条路径）。"""
    import pymupdf

    document = pymupdf.open()
    try:
        for index in range(pages):
            page = document.new_page()
            page.insert_text((72, 72), f"page {index + 1}: retrieval augmented generation")
        return document.tobytes()
    finally:
        document.close()


def test_pdf_extract_stops_at_the_page_where_the_budget_runs_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """抽到一半超预算 → 停在那一页、如实标 ``extract_timeout``，并写清抽到第几页。

    用假时钟（每次读都前进 1 秒）钉住"逐页检查"这件事：真机正常路径只要 0.07–0.2s，
    靠墙钟等是**等不出**这条分支的 —— 而且 Windows 的 ``time.monotonic()`` 粒度约 15.6ms，
    把预算压到 0 也照样"没超"（实测踩过），所以这里必须注入时钟。
    """
    import scholarscout.tools.source_fetch as module

    class _TickingTime:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            self.now += 1.0
            return self.now

    monkeypatch.setattr(module, "time", _TickingTime())

    text, status, engine, detail = module._pdf_text(_text_pdf_bytes(3), budget_s=0.0)

    assert status == "extract_timeout"
    assert engine == "local"
    assert text == "", "没抽完就把半份正文当结果更糟（摘要会基于残缺正文）"
    assert "1/3" in detail, f"要写清抽到第几页停下的，实为：{detail}"


def test_extract_budget_comes_from_config_and_the_status_surfaces(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具层：抽取预算取自 ``fetch.extract_timeout_s``，超时状态如实进返回与库。

    （逐页判定的逻辑由上面那条假时钟用例钉；这条钉的是**接线**：配置项真被读了、
    ``extract_timeout`` 真能走到返回 —— 否则那个配置键就是安慰剂。）
    """
    import scholarscout.tools.source_fetch as module

    seen: dict[str, float] = {}

    def _extract_timeout(content: bytes, *, budget_s: float) -> tuple[str, str, str, str]:
        seen["budget_s"] = budget_s
        return "", "extract_timeout", "local", f"本地抽取超过 {budget_s:g}s（已抽 1/3 页）"

    monkeypatch.setattr(module, "_pdf_text", _extract_timeout)
    _redirect_home(monkeypatch, tmp_path)
    ctx.config = replace(
        ctx.config,
        features=replace(ctx.config.features, enable_mineru=False),
        fetch_cfg=replace(ctx.config.fetch_cfg, extract_timeout_s=12.5),
    )
    pdf = _text_pdf_bytes(3)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})

    tool_ctx = _tool_ctx(ctx, handler, llm=StubLlm(reply(SUMMARY_TEXT)))
    data = source_fetch(tool_ctx, url="https://example.com/long.pdf")["data"]

    assert seen["budget_s"] == 12.5, "预算必须来自配置，不能写死在一个函数里"
    assert data["extract_status"] == "extract_timeout"
    assert data["extract_engine"] == "local"
    assert "1/3" in str(data["extract_detail"])
    assert data["chars"] == 0
    assert (tmp_path / data["raw_path"]).is_file(), "没抽完也要留下原件"
    assert ctx.db.scalar_int("SELECT count(*) FROM documents") == 1, "如实登记，不假装成功"


def test_one_dead_source_does_not_kill_the_turn(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**整轮**这一层：一轮里取两条来源、第一条不可达 → 整轮照常收口。

    为什么必须在整轮钉：抓取阶段的 ``FetchError`` 以前会冒泡出工具边界，模型只看到一句异常，
    于是反复重试同一批不可达 URL，直到烧光工具预算并开始编造；而工具层回 ``ok=false``
    是唯一能同时做到"让模型停手 + 让其余来源照旧完成"的做法（真机 2026-10-12）。
    """
    from scholarscout.runtime import loop
    from scholarscout.state import sessions

    _redirect_home(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if "dead.example.com" in str(request.url):
            raise httpx.ConnectError("host unreachable", request=request)
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"})

    ctx.http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    # 桩模型的回复顺序：轮1 要工具 → 轮2 要工具 → （第二条抓完出摘要，也走这个桩）→ 轮3 收口
    ctx.llm = StubLlm(
        reply(
            "",
            tool_calls=[
                tool_call("source.fetch", '{"url":"https://dead.example.com/paper"}', call_id="c1")
            ],
        ),
        reply(
            "",
            tool_calls=[
                tool_call("source.fetch", '{"url":"https://docs.example.com/guide"}', call_id="c2")
            ],
        ),
        reply(SUMMARY_TEXT),
        reply("两条来源里有一条取不到，另一条已入库并出了摘要。"),
    )
    session_id = sessions.create_session(ctx.db, model=ctx.config.chat.model)

    result = loop.run_turn(ctx.turn_context(), session_id=session_id, text="把这两条都取回来")

    assert result.stop_reason == "completed", "单源失败不许把整轮判错"
    assert result.tool_calls == ["source.fetch", "source.fetch"], "两次调用都真的执行了"

    tool_texts = [
        str(message["content"])
        for call in ctx.llm.calls
        for message in call["messages"]
        if isinstance(message, dict) and message.get("role") == "tool"
    ]
    assert any("抓取失败" in text and "dead.example.com" in text for text in tool_texts), (
        "坏的那条要带原因回给模型，模型才会停手"
    )
    assert any("docs.example.com" in text for text in tool_texts)

    # 好的一条照旧入库出摘要；坏的一条不留半条脏数据
    assert ctx.db.scalar_int("SELECT count(*) FROM documents") == 1
    assert ctx.db.scalar_int("SELECT count(*) FROM summaries") == 1
    # 两次调用都记了 trace（第一条不是"崩了"：工具自己回的 ok=false，没有异常逃出去）
    assert (
        ctx.db.scalar_int(
            "SELECT count(*) FROM tool_traces WHERE tool_name = 'source.fetch' AND error IS NULL"
        )
        == 2
    )


# --------------------------------------------------------------- 抓正文的按 host 限速


class _FakeClock:
    """假时钟：``sleep`` 只把时间往前拨 —— 测试**不真的等** 1 秒 / 3 秒。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _throttled_ctx(ctx: AppContext, slept: list[float]) -> ToolContext:
    """装一个注入了假时钟 / 假 sleep 的 ToolContext（限速走了多久可断言，网络仍离线）。"""
    clock = _FakeClock()

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"})

    ctx.http = HttpClient(
        respect_robots=False, transport=httpx.MockTransport(handler), clock=clock, sleep=sleep
    )
    return ToolContext(db=ctx.db, config=ctx.config, http=ctx.http, llm=None, session_id="ses_t")


def test_body_fetch_waits_the_host_min_interval(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 host 连抓两次，第二次必须等满最小间隔 —— 限速不再只作用于检索。

    用注入的假时钟 + 假 sleep 断言"等了多久"（``slept``），不真的等；
    ``docs.example.com`` 不在 ``search.min_interval_s`` 表里 → 走 1s 默认值。
    """
    _redirect_home(monkeypatch, tmp_path)
    slept: list[float] = []
    tool_ctx = _throttled_ctx(ctx, slept)

    first = source_fetch(tool_ctx, url="https://docs.example.com/one")
    second = source_fetch(tool_ctx, url="https://docs.example.com/two")

    assert first["ok"] is True and second["ok"] is True
    assert slept == [pytest.approx(1.0)], "同一 host 的第二次抓取没等满 min_interval_s"


def test_arxiv_body_fetch_uses_the_three_second_interval(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """arXiv 抓正文同样是 3s（表里已有的那条）—— 不是只有检索才守 3s。"""
    _redirect_home(monkeypatch, tmp_path)
    slept: list[float] = []
    tool_ctx = _throttled_ctx(ctx, slept)

    source_fetch(tool_ctx, url="https://arxiv.org/abs/2501.00001")
    source_fetch(tool_ctx, url="https://arxiv.org/abs/2501.00002")

    # 第一次不必等；第二次落在同一 host（export.arxiv.org 也算 arxiv）
    assert slept == [pytest.approx(3.0)]


# --------------------------------------------------------------- `.ts` 不许被当成视频

TS_SOURCE = "export interface Paper { id: string }\nexport const answer: number = 42;\n"


def test_ts_served_as_text_is_read_as_source(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.ts` + `text/plain` → 按纯文本抽正文。

    `.ts` 同时是 TypeScript 与 MPEG-TS 视频的后缀，判错就是**整片正文被静默丢掉**
    （标成"音视频不抽正文"）。所以这里钉到 `source.fetch` 的输出上：类型是 ts、走纯文本读取、
    正文落盘后能读到源码。
    """
    _redirect_home(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=TS_SOURCE, headers={"content-type": "text/plain; charset=utf-8"}
        )

    tool_ctx = _tool_ctx(ctx, handler, llm=StubLlm(reply(SUMMARY_TEXT)))

    result = source_fetch(tool_ctx, url="https://docs.example.com/src/worker.ts")
    data = result["data"]

    assert data["ext"] == "ts"
    assert data["extract_status"] == "ok", "TypeScript 源码不许被当成音视频跳过"
    assert data["extract_detail"] == "按纯文本读取"
    assert data["chars"] == len(TS_SOURCE)
    # 落盘后缀必须是 .ts（不能用视频的扩展名），否则后续按扩展名分派又会走错
    assert data["raw_path"].startswith("data/raw/")
    assert data["raw_path"].endswith(".ts")
    assert "export const answer" in (tmp_path / data["text_path"]).read_text(encoding="utf-8")


def test_ts_served_as_mpeg_video_is_skipped_as_media(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.ts` + `video/mp2t` → 媒体（这条才是真的视频，照旧不抽正文）。"""
    _redirect_home(monkeypatch, tmp_path)
    video = bytes([0x47]) + b"\x00" * 187 + bytes([0x47]) + b"\x00" * 187

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=video, headers={"content-type": "video/mp2t"})

    tool_ctx = _tool_ctx(ctx, handler)

    data = source_fetch(tool_ctx, url="https://cdn.example.com/clip.ts")["data"]

    assert data["ext"] == "media"
    assert data["extract_status"] == "skipped_media"
    assert "音视频不提取正文" in data["extract_detail"]


def test_video_body_is_never_downloaded(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个 15MB 的 mp4：`Content-Type` 一判就结束 —— 不读 body、不落"0 字节原件"、不登记文档。

    改前是"整包下完再发现是音视频"：用户看到的是 skipped_media，账单上是 15MB 流量，
    盘上还多一个假原件（`documents` 里那行指向它 → 下载按钮点开是空文件）。
    """
    _redirect_home(monkeypatch, tmp_path)
    payload = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 15_000_000
    pulled: list[int] = []

    class _CountingStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            pulled.append(len(payload))
            yield payload

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "video/mp4", "content-length": str(len(payload))},
            stream=_CountingStream(),
        )

    data = source_fetch(_tool_ctx(ctx, handler), url="https://cdn.example.com/lecture.mp4")["data"]

    assert pulled == [], "音视频正文一个字节都不许下载"
    assert data["ext"] == "media"
    assert data["extract_status"] == "skipped_media"
    assert data["bytes"] == len(payload), "大小按 Content-Length 如实登记，不编也不漏"
    assert data["raw_path"] == "" and data["text_path"] == "", "没正文就没有原件路径"
    assert ctx.db.scalar_int("SELECT count(*) FROM documents") == 0, "没下正文不许登记文档"
    assert ctx.db.scalar_int("SELECT count(*) FROM sources") == 1, "来源本身仍要登记"


# --------------------------------------------------------------- 老格式不许假装能读

#: OLE 复合文档（.doc/.xls/.ppt）的文件头：D0 CF 11 E0，不是 zip 的 PK。
OLE_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504


@pytest.mark.parametrize(
    ("url", "content_type", "ext"),
    [
        ("https://files.example.com/report.doc", "application/msword", "doc"),
        ("https://files.example.com/grade.xls", "application/vnd.ms-excel", "xls"),
        ("https://files.example.com/deck.ppt", "application/vnd.ms-powerpoint", "ppt"),
    ],
)
def test_legacy_office_is_refused_with_an_actionable_reason(
    ctx: AppContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    content_type: str,
    ext: str,
) -> None:
    """`.doc/.xls/.ppt` → 明确"需先转换"，**不许**走进 zip+XML 或 HTML 提取器。

    原来 `.xls` 被改写成 xlsx 去解 zip（报"不是有效的 .xlsx"），`.doc/.ppt` 无后缀时更糟：
    二进制进 HTML 提取器，洗出一段乱码正文还标 `ok` —— 用户完全看不出到底发生了什么。
    """
    _redirect_home(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=OLE_HEADER, headers={"content-type": content_type})

    tool_ctx = _tool_ctx(ctx, handler, llm=StubLlm(reply(SUMMARY_TEXT)))

    data = source_fetch(tool_ctx, url=url)["data"]

    assert data["ext"] == ext, "老格式不许被折成 docx/xlsx/pptx，也不许变成 html/bin"
    assert data["extract_status"] == "unsupported_legacy_office"
    assert data["chars"] == 0, "读不了就别硬塞一段乱码当正文"
    assert "老格式（.doc/.xls/.ppt）需先转换为 .docx/.xlsx/.pptx" in data["extract_detail"]
    assert (tmp_path / data["text_path"]).read_text(encoding="utf-8") == ""

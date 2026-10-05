"""``source.grade`` 契约：判权落库、反范式化、topic 相关性与模型路径、只追加。"""

from __future__ import annotations

import json

import pytest

from scholarscout.rpc.methods import AppContext
from scholarscout.state.ingest import latest_grade, list_grades, upsert_source
from scholarscout.tools import register_tools
from scholarscout.tools.source_grade import PARAMETERS, SPEC, source_grade

from ..conftest import StubLlm, reply

ARXIV_ROW = {
    "url": "https://arxiv.org/abs/2501.00001",
    "url_canonical": "https://arxiv.org/abs/2501.00001",
    "domain": "arxiv.org",
    "title": "教育智能体的检索增强方法",
    "source_type": "arxiv",
    "year": 2025,
    "venue": "arXiv",
}
BLOG_ROW = {
    "url": "https://blog.csdn.net/x/article",
    "url_canonical": "https://blog.csdn.net/x/article",
    "domain": "blog.csdn.net",
    "title": "随手记：某框架试用手记",
    "source_type": "web",
    "year": 2018,
}


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


def _seed(ctx: AppContext) -> tuple[str, str]:
    arxiv_id, _ = upsert_source(ctx.db, ARXIV_ROW)
    blog_id, _ = upsert_source(ctx.db, BLOG_ROW)
    return arxiv_id, blog_id


def test_spec_shape() -> None:
    assert SPEC.name == "source.grade"
    assert PARAMETERS["additionalProperties"] is False
    assert set(PARAMETERS["properties"]) == {"source_ids", "urls", "topic", "use_model", "limit"}


def test_grade_without_topic_uses_authority_only(ctx: AppContext) -> None:
    arxiv_id, blog_id = _seed(ctx)
    result = source_grade(ctx_tool(ctx), source_ids=[arxiv_id, blog_id])

    items = {item["source_id"]: item for item in result["data"]["items"]}
    assert items[arxiv_id]["tier"] == 1
    assert items[arxiv_id]["authority"] == 5.0
    assert items[arxiv_id]["verdict"] == "相关"
    assert items[arxiv_id]["relevance"] is None
    assert items[arxiv_id]["relevance_source"] == "none"
    assert items[blog_id]["tier"] == 4
    assert items[blog_id]["authority"] == 1.0
    assert items[blog_id]["verdict"] == "排除", "低质来源即便不判相关性也应被排除"
    assert result["data"]["relevance_engine"] == "none"


def test_grade_updates_sources_table(ctx: AppContext) -> None:
    arxiv_id, _ = _seed(ctx)
    source_grade(ctx_tool(ctx), source_ids=[arxiv_id])
    row = ctx.db.query_one("SELECT tier, authority FROM sources WHERE id = ?", (arxiv_id,))
    assert row is not None
    assert row["tier"] == 1
    assert row["authority"] == 5.0


def test_grade_with_topic_and_no_llm_uses_local_engine(ctx: AppContext) -> None:
    arxiv_id, _ = _seed(ctx)
    ctx.llm = None  # 没有模型时照样能判相关性（确定性打分）
    result = source_grade(ctx_tool(ctx), source_ids=[arxiv_id], topic="教育智能体")
    item = result["data"]["items"][0]
    assert item["relevance_source"] == "local"
    assert item["relevance"] is not None and item["relevance"] >= 3.0
    assert result["data"]["relevance_engine"] == "local"


def test_grade_with_model_scores_relevance(ctx: AppContext) -> None:
    arxiv_id, _ = _seed(ctx)
    ctx.llm = StubLlm(reply("relevance: 5\nreason: 标题直接命中教育智能体与检索增强"))
    result = source_grade(ctx_tool(ctx), source_ids=[arxiv_id], topic="教育智能体")
    item = result["data"]["items"][0]
    assert item["relevance_source"] == "model"
    assert item["relevance"] == 5.0
    assert item["verdict"] == "核心"
    assert item["reason"] is not None and "直接命中" in item["reason"]


def test_use_model_false_forces_local(ctx: AppContext) -> None:
    arxiv_id, _ = _seed(ctx)
    ctx.llm = StubLlm()  # 不应被调用
    result = source_grade(ctx_tool(ctx), source_ids=[arxiv_id], topic="教育智能体", use_model=False)
    assert result["data"]["items"][0]["relevance_source"] == "local"


def test_grade_by_url_uses_canonical_lookup(ctx: AppContext) -> None:
    arxiv_id, _ = _seed(ctx)
    result = source_grade(ctx_tool(ctx), urls=["https://arxiv.org/abs/2501.00001?utm_source=x"])
    assert [item["source_id"] for item in result["data"]["items"]] == [arxiv_id]


def test_unknown_ids_are_reported_as_skipped(ctx: AppContext) -> None:
    result = source_grade(
        ctx_tool(ctx), source_ids=["src_nope"], urls=["https://nowhere.example/x"]
    )
    assert result["data"]["items"] == []
    reasons = {entry["reason"] for entry in result["data"]["skipped"]}
    assert reasons == {"not_found"}
    assert len(result["data"]["skipped"]) == 2


def test_without_arguments_grades_untiered_sources(ctx: AppContext) -> None:
    arxiv_id, blog_id = _seed(ctx)
    result = source_grade(ctx_tool(ctx))
    graded = {item["source_id"] for item in result["data"]["items"]}
    assert graded == {arxiv_id, blog_id}
    # 再跑一次：都判过了，不该重复判（tier 已非空）
    again = source_grade(ctx_tool(ctx))
    assert again["data"]["items"] == []


def test_grades_are_append_only(ctx: AppContext) -> None:
    arxiv_id, _ = _seed(ctx)
    source_grade(ctx_tool(ctx), source_ids=[arxiv_id], topic="主题A")
    source_grade(ctx_tool(ctx), source_ids=[arxiv_id], topic="主题B")
    assert ctx.db.scalar_int("SELECT count(*) FROM source_grades") == 2
    assert latest_grade(ctx.db, arxiv_id)["topic"] == "主题B"
    assert latest_grade(ctx.db, arxiv_id, topic="主题A")["topic"] == "主题A"


def test_grade_row_records_rule_and_signals(ctx: AppContext) -> None:
    arxiv_id, _ = _seed(ctx)
    source_grade(ctx_tool(ctx), source_ids=[arxiv_id], topic="教育智能体")
    row = latest_grade(ctx.db, arxiv_id)
    assert row is not None
    assert row["matched_rule_id"] is not None
    assert row["tier_label"] == "一手权威"
    signals = json.loads(str(row["signals_json"]))
    names = [signal["name"] for signal in signals]
    assert "matched_rule" in names
    assert "topic_coverage" in names


def test_counts_and_refs(ctx: AppContext) -> None:
    arxiv_id, blog_id = _seed(ctx)
    result = source_grade(ctx_tool(ctx), source_ids=[arxiv_id, blog_id], topic="教育智能体")
    assert result["refs"]["source_ids"] == [arxiv_id, blog_id]
    assert sum(result["data"]["counts"].values()) == 2


def test_list_grades_filters_by_verdict(ctx: AppContext) -> None:
    arxiv_id, blog_id = _seed(ctx)
    source_grade(ctx_tool(ctx), source_ids=[arxiv_id, blog_id])
    assert len(list_grades(ctx.db)) == 2
    assert len(list_grades(ctx.db, verdict="排除")) == 1
    assert len(list_grades(ctx.db, verdict="核心")) == 0


def test_relevance_uses_summary_when_available(ctx: AppContext) -> None:
    """有中文摘要时应把摘要文本算进相关性判断（主题词多为中文）。"""
    arxiv_id, _ = _seed(ctx)
    ctx.db.execute(
        """
        INSERT INTO documents (id, source_id, sha256, fetched_at) VALUES ('doc_g', ?, 'aa', '2026-10-05T10:00:00+08:00')
        """,
        (arxiv_id,),
    )
    ctx.db.execute(
        """
        INSERT INTO summaries (id, doc_id, source_id, kind, title_zh, summary_short, prompt_version, created_at)
        VALUES ('sum_g', 'doc_g', ?, 'paper', '教育智能体综述', '该综述系统梳理教育智能体的检索增强方法。', 'v1', '2026-10-05T10:00:00+08:00')
        """,
        (arxiv_id,),
    )
    result = source_grade(ctx_tool(ctx), source_ids=[arxiv_id], topic="教育智能体", use_model=False)
    item = result["data"]["items"][0]
    assert item["relevance"] == 5.0, "标题与摘要都命中主题，本地分应打满"


def ctx_tool(ctx: AppContext):
    from scholarscout.runtime.toolctx import ToolContext

    return ToolContext(db=ctx.db, config=ctx.config, http=ctx.http, llm=ctx.llm)

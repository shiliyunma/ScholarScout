"""端到端：模型要求 ``research.search`` → loop 真调用 → 结果喂回模型 → 落库落轨迹。

这条用例是 M3 的验收线：**模型能真的找到资料并带着 source_id 回答**，而不是假装调用。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scholarscout.fetch.http import HttpClient
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.tools import register_tools

from ..conftest import StubLlm, reply, tool_call

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "arxiv_sample.xml"
PAGE = (
    "<html><head><title>教育智能体入门</title></head><body><main>"
    + "<p>教育智能体先检索再作答，示例在 MATH 上把准确率从 42% 提升到 58%。</p>" * 20
    + "</main></body></html>"
)
SUMMARY_TEXT = (
    "title_zh: 教育智能体入门\n"
    "summary_short: 这份材料给出教育智能体的入门路径，先把检索层做对再做工具调用，"
    "并报告在 MATH 数据集上把准确率从 42% 提升到 58%，附有可运行的命令。\n"
)


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "export.arxiv.org" in url:
        return httpx.Response(
            200,
            text=FIXTURE.read_text(encoding="utf-8"),
            headers={"content-type": "application/atom+xml"},
        )
    return httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"})


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


def test_search_then_answer_with_source_ids(ctx: AppContext) -> None:
    ctx.http = HttpClient(respect_robots=False, transport=httpx.MockTransport(_handler))
    stub = StubLlm(
        reply(
            "",
            tool_calls=[tool_call("research.search", '{"query": "education agent", "limit": 2}')],
        ),
        reply("找到了 2 篇 arXiv 论文，我先按来源分层整理给你。", tool_calls=[]),
    )
    ctx.llm = stub
    session_id = dispatch(ctx, "session.create", {})["session_id"]

    result = dispatch(
        ctx, "chat.send", {"session_id": session_id, "text": "帮我找教育智能体的入门资料"}
    )

    assert result["stop_reason"] == "completed"
    assert result["tool_calls"] == ["research.search"]
    assert result["text"].startswith("找到了 2 篇")

    # 真的入库了
    assert ctx.db.scalar_int("SELECT count(*) FROM sources") == 2
    # 轨迹记录了成功调用
    traces = dispatch(ctx, "trace.list", {})["items"]
    assert len(traces) == 1
    assert traces[0]["tool_name"] == "research.search"
    assert traces[0]["ok"] == 1

    # 工具结果真的喂回了模型，且带可追溯的 source_id
    follow_up_messages = stub.calls[1]["messages"]
    tool_message = follow_up_messages[-1]
    assert tool_message["role"] == "tool"
    payload = json.loads(str(tool_message["content"]))
    assert payload["ok"] is True
    assert all(item["source_id"].startswith("src_") for item in payload["data"]["items"])
    # 结论里的出处：库里能按 source_id 找到
    first_id = payload["data"]["items"][0]["source_id"]
    assert ctx.db.query_one("SELECT url FROM sources WHERE id = ?", (first_id,)) is not None


def test_search_then_fetch_then_summarize(ctx: AppContext, tmp_path: Path, monkeypatch) -> None:
    """完整链路：检索 → 取原文 → 出摘要 → 摘要可被检索。"""
    monkeypatch.setenv("SCHOLARSCOUT_HOME", str(tmp_path))
    ctx.http = HttpClient(respect_robots=False, transport=httpx.MockTransport(_handler))
    stub = StubLlm(
        reply("", tool_calls=[tool_call("research.search", '{"query": "education agent"}')]),
        reply(
            "",
            tool_calls=[
                tool_call(
                    "source.fetch", '{"url": "https://arxiv.org/abs/2501.00001"}', call_id="c2"
                )
            ],
        ),
        reply("已按来源分层整理，并生成了带数字的摘要。", tool_calls=[]),
    )
    stub.replies.insert(2, reply(SUMMARY_TEXT))  # source.fetch 内部的摘要调用
    ctx.llm = stub
    session_id = dispatch(ctx, "session.create", {})["session_id"]

    result = dispatch(ctx, "chat.send", {"session_id": session_id, "text": "消化一篇论文再讲给我"})

    assert result["stop_reason"] == "completed"
    assert result["tool_calls"] == ["research.search", "source.fetch"]
    assert ctx.db.scalar_int("SELECT count(*) FROM documents") == 1
    assert ctx.db.scalar_int("SELECT count(*) FROM summaries") == 1
    assert ctx.db.scalar_int("SELECT count(*) FROM source_mentions") == 1
    assert ctx.db.scalar_int("SELECT count(*) FROM tool_traces") == 2

    from scholarscout.state.search import search_summaries

    assert search_summaries(ctx.db, "教育智能体"), "摘要写进库后必须可检索"

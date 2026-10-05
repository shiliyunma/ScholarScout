"""端到端：模型要求 ``research.search`` → loop 真调用 → 结果喂回模型 → 落库落轨迹。

这条用例是 M3 的验收线：**模型能真的找到资料并带着 source_id 回答**，而不是假装调用。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scholarscout import paths
from scholarscout.fetch.http import HttpClient
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.tools import register_tools
from scholarscout.tools.route_plan import route_plan
from scholarscout.tools.topic_map import topic_build_map

from ..conftest import StubLlm, reply, tool_call, tool_ctx

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


def test_full_chain_from_search_to_route_and_cards(
    ctx: AppContext, tmp_path: Path, monkeypatch
) -> None:
    """M1→M5 主线：检索 → 取原文出摘要 → 判权 → 主题地图 → 学习路线 → 抽背卡 → 导出。

    这条用例是毕设演示的主线，验收的是**每一件产物都挂在库里的真实资料上**：
    判权有留痕、地图的节点有出处、路线的每个条目都能指回 source_id、卡片能反查到资料。
    """
    monkeypatch.setenv("SCHOLARSCOUT_HOME", str(tmp_path))
    ctx.http = HttpClient(respect_robots=False, transport=httpx.MockTransport(_handler))

    # 第一回合：模型自己决定 检索 → 取原文 → 判权（恰好用满本回合的 3 次工具上限）
    stub = StubLlm(
        reply(
            "",
            tool_calls=[tool_call("research.search", '{"query": "education agent", "limit": 2}')],
        ),
        reply(
            "",
            tool_calls=[
                tool_call(
                    "source.fetch", '{"url": "https://arxiv.org/abs/2501.00001"}', call_id="c2"
                )
            ],
        ),
        reply(SUMMARY_TEXT),  # source.fetch 内部的摘要调用
        reply(
            "",
            tool_calls=[
                tool_call(
                    "source.grade",
                    '{"urls": ["https://arxiv.org/abs/2501.00001"], "topic": "教育智能体"}',
                    call_id="c3",
                )
            ],
        ),
        reply("relevance: 5\nreason: 直接讲教育智能体的检索层"),  # source.grade 的相关性打分
        reply("资料已按来源分层，可以据此排路线了。"),
    )
    ctx.llm = stub
    session_id = dispatch(ctx, "session.create", {})["session_id"]
    result = dispatch(ctx, "chat.send", {"session_id": session_id, "text": "找资料并判权威性"})

    assert result["stop_reason"] == "completed"
    assert result["tool_calls"] == ["research.search", "source.fetch", "source.grade"]

    # 判权：有留痕、有理由、tier 反范式化回 sources
    grade = ctx.db.query_one("SELECT * FROM source_grades")
    assert grade is not None
    assert grade["tier"] == 1
    assert grade["verdict"] in {"核心", "相关"}
    assert grade["relevance_source"] == "model"
    source_id = str(grade["source_id"])
    graded = ctx.db.query_one("SELECT * FROM sources WHERE id = ?", (source_id,))
    assert graded is not None and graded["tier"] == 1 and float(graded["authority"]) > 0

    # 第二回合：地图与路线（模型输出里故意混一个假 id，验证守卫真的拦得住）
    ctx.llm = StubLlm(
        reply(
            f"node: n1 | 教育智能体 | concept | {source_id}\n"
            "node: n2 | 凭空想的概念 | concept | src_不存在\n"
            "edge: n1 -> n2 | uses\n"
        ),
        reply(
            f"week: 1 | 能跑通检索 | 能解释每一步在做什么\n"
            f"item: paper | 教育智能体入门 | {source_id} | 先读它再动手 | 60\n"
        ),
    )
    topic = topic_build_map(tool_ctx(ctx), "教育智能体", max_nodes=5)
    plan = route_plan(tool_ctx(ctx), "教育智能体", weeks=2)

    assert topic["data"]["source_count"] >= 1, "候选来源应取到刚才判权过的资料"
    assert topic["data"]["stats"]["ungrounded_nodes"] == 1, "假 id 的节点必须被标 [无出处]"
    grounded = [n for n in topic["data"]["nodes"] if not str(n["label"]).startswith("[无出处]")]
    assert grounded and all(source_id in n["source_ids"] for n in grounded)

    items = [item for week in plan["data"]["weeks"] for item in week["items"]]
    assert items and all(item["source_id"] == source_id for item in items), "路线条目必须挂真实资料"

    # 第三回合：把要点做成抽背卡（引用真实 source_id）
    card_args = json.dumps(
        {
            "kind": "card",
            "cards": [
                {"front": "教育智能体先做哪一层", "back": f"先做检索层（{source_id}）"},
                {"front": "摘要里的准确率提升是多少", "back": "MATH 上 42% → 58%"},
            ],
            "refs": {"source_ids": [source_id]},
            "tags": ["教育智能体"],
        },
        ensure_ascii=False,
    )
    ctx.llm = StubLlm(
        reply("", tool_calls=[tool_call("note.save", card_args, call_id="c4")]),
        reply("卡片已存好，可以开始抽背了。"),
    )
    card_turn = dispatch(ctx, "chat.send", {"session_id": session_id, "text": "把要点做成抽背卡"})
    assert card_turn["tool_calls"] == ["note.save"]

    from scholarscout.state.learning import notes_for_source

    cards = notes_for_source(ctx.db, source_id)
    assert len(cards) == 1 and cards[0]["kind"] == "card"
    card_file = Path(str(cards[0]["path"]))
    assert (paths.home() / card_file).is_file(), "抽背卡要真的落到 data/notes/"

    # 导出：演示时直接给人看的 Markdown
    exported = dispatch(ctx, "route.export", {"route_id": plan["data"]["route_id"]})
    body = (paths.home() / exported["path"]).read_text(encoding="utf-8")
    assert source_id in body and "**检查点**" in body

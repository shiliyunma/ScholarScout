"""模型摘要式压缩（D3）的口径测试。

最要紧的一条是**成本**：同一段历史只压一次。不然"省 token"的功能会变成每轮多花一次调用。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from scholarscout.rpc.methods import AppContext
from scholarscout.runtime import compaction, loop
from scholarscout.runtime import research_state as rs
from scholarscout.state import sessions
from scholarscout.state.db import Database

PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


class StubLlm:
    def __init__(self, *contents: str) -> None:
        self.contents = list(contents)
        self.calls: list[list[dict]] = []

    def chat(self, messages, *, tools=None):
        self.calls.append(list(messages))
        content = self.contents.pop(0) if self.contents else "（没有脚本回复了）"

        class _Reply:
            pass

        reply = _Reply()
        reply.content = content
        reply.usage = {"prompt_tokens": 900, "completion_tokens": 180}
        return reply


def _session(db: Database, texts: list[tuple[str, str]]) -> str:
    session_id = sessions.create_session(db, model="stub")
    for index, (role, content) in enumerate(texts):
        db.execute(
            "INSERT INTO messages (id, session_id, idx, role, content, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                f"m_{session_id}_{index}",
                session_id,
                index,
                role,
                content,
                "2026-10-07T00:00:00+08:00",
            ),
        )
    return session_id


# ------------------------------------------------------------------ 跨度与阈值


def test_pending_span_skips_what_is_already_covered(db: Database) -> None:
    session_id = _session(db, [("user", "第一句"), ("assistant", "答一"), ("user", "第二句")])
    assert [item["content"] for item in compaction.pending_span(db, session_id)] == [
        "第一句",
        "答一",
        "第二句",
    ]
    state = {compaction.COVERED_KEY: 2}
    assert [item["content"] for item in compaction.pending_span(db, session_id, state=state)] == [
        "第二句"
    ]


def test_should_compact_only_above_threshold(db: Database) -> None:
    short = _session(db, [("user", "短")])
    assert compaction.should_compact(db, short, after_chars=1200) is False

    long = _session(db, [("user", "很长的话" * 400)])
    assert compaction.should_compact(db, long, after_chars=1200) is True


# ------------------------------------------------------------------ 压缩本身


def test_compact_summarizes_and_remembers_the_span(db: Database) -> None:
    session_id = _session(db, [("user", "第一轮问题"), ("assistant", "第一轮回答")])
    llm = StubLlm("### 目标\n搞懂 GraphRAG")
    outcome = compaction.compact(llm, db, session_id, prompts_dir=PROMPTS)
    assert outcome["summary"].startswith("### 目标")
    assert outcome["covered"] == 2
    assert outcome["usage"]["prompt_tokens"] == 900
    compaction.save_summary(
        db, session_id, summary=str(outcome["summary"]), covered=int(outcome["covered"])
    )

    # **同一段不再压第二次**（这是这个功能会不会变成暗烧钱的关键）
    again = compaction.compact(llm, db, session_id, prompts_dir=PROMPTS)
    assert again["summary"] is None and "没有待压缩" in str(again["reason"])
    assert len(llm.calls) == 1, "同一段历史只能压一次"


def test_compact_continues_from_the_previous_summary(db: Database) -> None:
    """第二次压缩是**续写**：把上次的提要一起给它，而不是从零重来。"""
    session_id = _session(db, [("user", "第一轮")])
    llm = StubLlm("第一版提要", "第二版提要")
    first = compaction.compact(llm, db, session_id, prompts_dir=PROMPTS)
    compaction.save_summary(db, session_id, summary=str(first["summary"]), covered=1)

    db.execute(
        "INSERT INTO messages (id, session_id, idx, role, content, created_at) VALUES (?,?,?,?,?,?)",
        ("m9", session_id, 9, "user", "第二轮", "2026-10-07T00:00:00+08:00"),
    )
    second = compaction.compact(llm, db, session_id, prompts_dir=PROMPTS)
    assert second["summary"] == "第二版提要" and second["covered"] == 2
    assert "第一版提要" in str(llm.calls[1][0]["content"])


def test_compact_failure_is_reported_not_raised(db: Database) -> None:
    session_id = _session(db, [("user", "第一轮")])

    class Boom:
        def chat(self, messages, *, tools=None):
            raise RuntimeError("模型不可达")

    outcome = compaction.compact(Boom(), db, session_id, prompts_dir=PROMPTS)
    assert outcome["summary"] is None and "模型不可达" in str(outcome["reason"])


def test_empty_reply_is_not_saved(db: Database) -> None:
    session_id = _session(db, [("user", "第一轮")])
    outcome = compaction.compact(StubLlm("   "), db, session_id, prompts_dir=PROMPTS)
    assert outcome["summary"] is None and outcome["reason"] == "模型返回空提要"


# ------------------------------------------------------------------ 台账与注入


def test_save_summary_keeps_the_rest_of_the_ledger(db: Database) -> None:
    """提要存进研究台账，**不能把台账已有的东西（目标/子问题/已搜查询）冲掉**。"""
    state = rs.empty_state("搞懂 GraphRAG")
    state["questions"] = [{"text": "是什么", "status": "open", "evidence": []}]
    rs.record_query(state, "GraphRAG 原理")
    rs.save_state(db, "ses_x", state)

    compaction.save_summary(db, "ses_x", summary="### 目标\n搞懂 GraphRAG", covered=4)
    after = rs.load_state(db, "ses_x") or {}
    assert after["goal"] == "搞懂 GraphRAG"
    assert after["questions"][0]["text"] == "是什么"
    assert after["used_queries"] == ["GraphRAG 原理"]
    assert after[compaction.SUMMARY_KEY].startswith("### 目标")
    assert after[compaction.COVERED_KEY] == 4


def test_summary_note_is_injected_right_after_the_system_prompt(ctx: AppContext) -> None:
    """位置固定在 system 之后 —— 位置固定，缓存才能在两次压缩之间稳定命中。"""
    session_id = _session(ctx.db, [("user", "第一轮问题")])
    config = ctx.config
    before = loop.build_messages(ctx.db, session_id, config=config, toolsets=["research"])
    assert not any("较早对话的提要" in str(m.get("content")) for m in before)

    compaction.save_summary(ctx.db, session_id, summary="### 目标\n搞懂 GraphRAG", covered=1)
    after = loop.build_messages(ctx.db, session_id, config=config, toolsets=["research"])
    assert after[0]["role"] == "system"
    assert "较早对话的提要" in str(after[1]["content"])
    assert "搞懂 GraphRAG" in str(after[1]["content"])


def test_note_says_only_trust_cited_conclusions() -> None:
    note = compaction.render_summary_note("### 目标\nX")
    assert "带来源 id" in note


# ------------------------------------------------------------------ 端到端（轮级）


def test_turn_triggers_one_compaction_and_records_the_cost(ctx: AppContext) -> None:
    """真跑一轮：超阈值就压一次，开销记成 purpose=compact；第二轮不重复压。"""
    session_id = _session(
        ctx.db,
        [
            ("user", "第一轮：帮我搞懂 GraphRAG 的本质 " * 60),
            ("assistant", "好的，先给结论（来源 src_abc123）…… " * 60),
        ],
    )
    ctx.config = replace(ctx.config, runtime=replace(ctx.config.runtime, compact_after_chars=1200))
    ctx.llm = StubLlm("### 目标\n搞懂 GraphRAG（来源 src_abc123）", "答复一", "答复二")

    loop.run_turn(ctx.turn_context(), session_id=session_id, text="第二轮")
    state = rs.load_state(ctx.db, session_id) or {}
    assert str(state.get(compaction.SUMMARY_KEY) or "").startswith("### 目标")
    assert int(state.get(compaction.COVERED_KEY) or 0) >= 2
    usage = ctx.db.query(
        "SELECT purpose, count(*) AS n FROM usage_records WHERE session_id = ? GROUP BY purpose",
        (session_id,),
    )
    assert {str(row["purpose"]) for row in usage} >= {"compact"}

    # 第二轮：没有新的未覆盖内容 → 不该再压一次
    calls_before = len(ctx.llm.calls)
    loop.run_turn(ctx.turn_context(), session_id=session_id, text="第三轮")
    compactions = [
        call
        for call in ctx.llm.calls[calls_before:]
        if any("较长的会话历史" in str(m.get("content") or "") for m in call)
    ]
    assert compactions == [], "没有新增未覆盖内容时不该再花一次压缩调用"

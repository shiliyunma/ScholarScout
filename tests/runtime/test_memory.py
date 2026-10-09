"""跨会话记忆（D2）的口径测试。

重点钉住"不会变成污染源"的那几条：只抽稳定事实、入库前判重、**能删能改**、
冲突以用户当前说法为准、以及"没有可记的事就回空表是对的"。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.runtime import memory
from scholarscout.state.db import Database

PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


class StubLlm:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[list[dict]] = []

    def chat(self, messages, *, tools=None):
        self.calls.append(list(messages))

        class _Reply:
            content = self.content
            usage: ClassVar[dict[str, int]] = {"prompt_tokens": 120, "completion_tokens": 40}

        return _Reply()


def _session(db: Database, session_id: str, user_messages: list[str]) -> str:
    db.execute(
        "INSERT INTO sessions (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
        (session_id, "测试会话", "stub", "2026-10-07T00:00:00+08:00", "2026-10-07T00:00:00+08:00"),
    )
    for index, text in enumerate(user_messages):
        db.execute(
            "INSERT INTO messages (id, session_id, idx, role, content, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                f"msg_{session_id}_{index}",
                session_id,
                index,
                "user",
                text,
                "2026-10-07T00:00:00+08:00",
            ),
        )
    return session_id


# ------------------------------------------------------------------ 存取与判重


def test_add_dedupes_near_identical_text(db: Database) -> None:
    """本地判重只兜**近似文本**（同一句话的复述/扩写），不是语义判重。

    扩写算重复："偏好中文" ⊂ "偏好中文回答，不喜欢深蓝"。
    """
    first = memory.add_fact(db, "用户偏好中文", kind="preference")
    assert first["created"] is True
    again = memory.add_fact(db, "用户偏好中文回答，不喜欢深蓝色界面", kind="preference")
    assert again["created"] is False
    assert again["fact"]["id"] == first["fact"]["id"]
    assert len(memory.list_facts(db)) == 1


def test_semantic_rephrasing_is_left_to_the_model(db: Database) -> None:
    """打乱语序的同义句本地抓不到（实测相似度仅 0.5），**这是有意的**：

    能抓乱序改写的"字符集重合度"会把"偏喜欢中文"和"不喜欢中文"判成同一条 ——
    误合并比漏合并危险。所以语义判重交给模型：抽取时把已有记忆一起给它。
    """
    memory.add_fact(db, "用户在写本科毕设，方向是 AI+教育", kind="fact")
    reworded = memory.add_fact(db, "用户在写 AI+教育 方向的本科毕设", kind="fact")
    assert reworded["created"] is True  # 本地放行
    assert len(memory.list_facts(db)) == 2
    # 但抽取请求里必须带上下已有的记忆，模型才能自己判重
    session_id = _session(db, "ses_ctx", ["一句", "两句"])
    llm = StubLlm('{"facts": []}')
    memory.extract_facts(llm, db, session_id, prompts_dir=PROMPTS)
    prompt = str(llm.calls[0][0]["content"])
    assert "用户在写本科毕设，方向是 AI+教育" in prompt


def test_add_rejects_empty_and_bad_kind(db: Database) -> None:
    with pytest.raises(ValueError):
        memory.add_fact(db, "   ")
    with pytest.raises(ValueError):
        memory.add_fact(db, "用户在写毕设", kind="guess")


def test_update_and_remove(db: Database) -> None:
    fact = memory.add_fact(db, "用户偏好中文", kind="preference")["fact"]
    memory.update_fact(db, fact["id"], "用户偏好中文，不喜欢深蓝色界面")
    assert "深蓝色" in memory.list_facts(db)[0]["text"]
    memory.remove_fact(db, fact["id"])
    assert memory.list_facts(db) == []
    with pytest.raises(ValueError):
        memory.remove_fact(db, fact["id"])


# ------------------------------------------------------------------ 解析


def test_parse_facts_handles_fence_bare_and_garbage() -> None:
    fenced = '```json\n{"facts": [{"text": "用户在写毕设", "kind": "fact"}]}\n```'
    assert memory.parse_facts(fenced) == [
        {"text": "用户在写毕设", "kind": "fact", "target": "user"}
    ]
    bare = '{"facts": [{"text": "偏好中文", "kind": "preference"}]}'
    assert memory.parse_facts(bare)[0]["kind"] == "preference"
    assert memory.parse_facts("模型跑题了") == []
    assert memory.parse_facts('{"facts": []}') == []
    # 类别写错 → 落到 fact，不丢这条
    assert memory.parse_facts('{"facts": [{"text": "x", "kind": "??"}]}')[0]["kind"] == "fact"


def test_parse_facts_caps_the_count() -> None:
    items = ",".join(f'{{"text": "事实 {i}", "kind": "fact"}}' for i in range(9))
    assert len(memory.parse_facts('{"facts": [' + items + "]}")) == memory.MAX_EXTRACTED


# ------------------------------------------------------------------ 注入


def test_render_note_is_empty_without_facts(db: Database) -> None:
    assert memory.render_facts_note(db) == ""


def test_render_note_carries_the_conflict_rule_and_respects_budget(db: Database) -> None:
    for index in range(20):
        memory.add_fact(db, f"偏好 {index}：" + "啰嗦" * 40, kind="preference")
    note = memory.render_facts_note(db)
    assert "以当前说法为准" in note
    assert len(note) < memory.NOTE_BUDGET_CHARS + 400  # 超预算只发最新的几条


def test_transcript_only_takes_user_messages_and_artifact_topics(db: Database) -> None:
    session_id = _session(db, "ses_m", ["第一句", "第二句"])
    db.execute(
        "INSERT INTO messages (id, session_id, idx, role, content, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            "msg_a",
            session_id,
            99,
            "assistant",
            "助手的长篇回答不该进素材",
            "2026-10-07T00:00:01+08:00",
        ),
    )
    db.execute(
        "INSERT INTO topic_maps (id, session_id, topic, graph_json, created_at) VALUES (?,?,?,?,?)",
        ("tm_m", session_id, "GraphRAG", "{}", "2026-10-07T00:00:00+08:00"),
    )
    text = memory.build_transcript(db, session_id)
    assert "第一句" in text and "第二句" in text
    assert "助手的长篇回答" not in text
    assert "GraphRAG" in text


# ------------------------------------------------------------------ 抽取


def test_extract_adds_new_and_skips_duplicates(db: Database) -> None:
    session_id = _session(db, "ses_e", ["我在写毕设", "顺便帮我看 GraphRAG"])
    memory.add_fact(db, "用户在写本科毕设", kind="fact")
    llm = StubLlm(
        '{"facts": [{"text": "用户在写本科毕设，方向 AI+教育", "kind": "fact"},'
        ' {"text": "用户偏好中文回答", "kind": "preference"}]}'
    )
    outcome = memory.extract_facts(llm, db, session_id, prompts_dir=PROMPTS)
    assert len(outcome["added"]) == 1 and outcome["added"][0]["kind"] == "preference"
    assert len(outcome["skipped"]) == 1
    assert outcome["usage"]["prompt_tokens"] == 120
    # 素材里只该有用户说过的话
    assert "我在写毕设" in str(llm.calls[0][0]["content"])


def test_extract_never_fails_the_turn(db: Database) -> None:
    session_id = _session(db, "ses_bad", ["随便说说"])
    outcome = memory.extract_facts(StubLlm("不是 JSON"), db, session_id, prompts_dir=PROMPTS)
    assert outcome["added"] == [] and outcome["reason"] is None
    assert memory.list_facts(db) == []


def test_should_extract_requires_signal_and_runs_once(db: Database) -> None:
    quiet = _session(db, "ses_quiet", ["只问了一句"])
    assert memory.should_extract(db, quiet) is False

    chatty = _session(db, "ses_chatty", ["第一句", "第二句"])
    assert memory.should_extract(db, chatty) is True
    memory.add_fact(db, "用户在写毕设", kind="fact", source_session=chatty)
    assert memory.should_extract(db, chatty) is False  # 每会话只抽一次


# ------------------------------------------------------------------ RPC


def test_memory_rpcs(ctx: AppContext) -> None:
    created = dispatch(ctx, "memory.add", {"text": "用户偏好中文", "kind": "preference"})
    assert created["created"] is True
    fact_id = created["fact"]["id"]
    listed = dispatch(ctx, "memory.list", {})
    assert [item["id"] for item in listed["items"]] == [fact_id]
    dispatch(ctx, "memory.update", {"fact_id": fact_id, "text": "用户偏好中文，讨厌深蓝色"})
    assert "深蓝色" in dispatch(ctx, "memory.list", {})["items"][0]["text"]
    dispatch(ctx, "memory.remove", {"fact_id": fact_id})
    assert dispatch(ctx, "memory.list", {})["items"] == []


def test_memory_remove_unknown_id_reports_not_found(ctx: AppContext) -> None:
    from scholarscout.rpc.errors import RpcError

    with pytest.raises(RpcError):
        dispatch(ctx, "memory.remove", {"fact_id": "mem_nope"})


# ------------------------------------------------------------------ 双账（target，P0）


def test_facts_default_to_user_ledger_and_render_two_sections(db: Database) -> None:
    """两本账分道：`user` 与 `memory` 各进一段；默认账本是 `user`（迁移前行为不变）。"""
    memory.add_fact(db, "用户偏好中文回答", kind="preference")
    memory.add_fact(db, "本机 arXiv 接口需 ≥3 秒限速，连续请求会 406", kind="fact", target="memory")
    rows = memory.list_facts(db)
    assert {row["target"] for row in rows} == {"user", "memory"}
    note = memory.render_facts_note(db)
    assert "【关于这个用户的长期记忆】" in note
    assert "【我自己的工作笔记】" in note
    # 用户那条在 user 段、工作笔记那条在 memory 段
    assert note.index("用户偏好中文回答") < note.index("【我自己的工作笔记】")


def test_add_rejects_bad_target(db: Database) -> None:
    with pytest.raises(ValueError):
        memory.add_fact(db, "用户在写毕设", target="guess")


def test_parse_facts_carries_target_and_falls_back_to_user() -> None:
    got = memory.parse_facts('{"facts": [{"text": "x", "kind": "fact", "target": "memory"}]}')
    assert got[0]["target"] == "memory"
    # 账本写错 → 落 user，不丢这条（与 kind 同款兜底）
    fallback = memory.parse_facts('{"facts": [{"text": "y", "kind": "fact", "target": "??"}]}')
    assert fallback[0]["target"] == "user"


def test_two_ledgers_share_the_same_600_budget(db: Database) -> None:
    """分道只是分区，**总预算字仍是 600**（每轮 token 不增）。"""
    for index in range(8):
        memory.add_fact(db, f"用户偏好 {index}：" + "啰嗦" * 40, kind="preference")
    for index in range(8):
        memory.add_fact(db, f"工作笔记 {index}：" + "啰嗦" * 40, kind="fact", target="memory")
    note = memory.render_facts_note(db)
    body = "\n".join(line for line in note.splitlines() if line.startswith("- "))
    assert len(body) <= memory.NOTE_BUDGET_CHARS


def test_render_note_appends_merge_hint_when_over_budget(db: Database) -> None:
    for index in range(20):
        memory.add_fact(db, f"偏好 {index}：" + "啰嗦" * 40, kind="preference")
    note = memory.render_facts_note(db)
    assert memory.MERGE_HINT in note
    assert len(memory.MERGE_HINT) <= 40  # 一行，且不喧宾夺主


def test_extract_returns_current_entries_instead_of_silently_dropping(db: Database) -> None:
    """注入口袋顶满时，抽取**不静默丢旧**：停手并回 current_entries 让上游合并。"""
    # 把注入口袋塞到顶：贪心按整行装，末行留白 < 一行 → 任何新条目都顶破 600 字。
    for index in range(20):
        memory.add_fact(db, f"偏好 {index:02d}：" + "啰嗦" * 39, kind="preference")
    session_id = _session(db, "ses_full", ["第一句", "第二句"])
    llm = StubLlm(
        '{"facts": [{"text": "用户新增的目标：这学期搞定图算法", "kind": "goal", "target": "user"}]}'
    )
    outcome = memory.extract_facts(llm, db, session_id, prompts_dir=PROMPTS)
    assert outcome["added"] == []
    assert outcome["current_entries"]  # 回的是现有全部，而不是把旧的悄悄挤掉
    assert "预算" in str(outcome["reason"])


def test_memory_rpc_add_and_list_carry_target(ctx: AppContext) -> None:
    created = dispatch(
        ctx,
        "memory.add",
        {"text": "本机 arXiv 接口需 ≥3 秒限速", "kind": "fact", "target": "memory"},
    )
    assert created["fact"]["target"] == "memory"
    listed = dispatch(ctx, "memory.list", {})
    assert listed["items"][0]["target"] == "memory"


def test_memory_rpc_rejects_bad_target(ctx: AppContext) -> None:
    from scholarscout.rpc.errors import RpcError

    with pytest.raises(RpcError):
        dispatch(ctx, "memory.add", {"text": "用户在写毕设", "target": "nope"})

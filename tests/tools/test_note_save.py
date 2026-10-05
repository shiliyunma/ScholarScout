"""``note.save`` 契约：三类产物、卡片批量、Markdown 落盘、引用留痕。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout import paths
from scholarscout.rpc.methods import AppContext
from scholarscout.state.learning import get_note, notes_for_source
from scholarscout.tools import register_tools
from scholarscout.tools.note_save import PARAMETERS, SPEC, note_save, render_cards

from ..conftest import redirect_home, tool_ctx


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return redirect_home(monkeypatch, tmp_path)


def test_spec_shape() -> None:
    assert SPEC.name == "note.save"
    assert PARAMETERS["additionalProperties"] is False
    assert set(PARAMETERS["properties"]) == {"kind", "title", "content", "cards", "refs", "tags"}


def test_render_cards_numbers_questions() -> None:
    text = render_cards([{"front": "什么是 RAG", "back": "先检索再生成"}])
    assert "## Q1. 什么是 RAG" in text
    assert "**A.** 先检索再生成" in text


def test_save_card_batch_writes_file_and_refs(ctx: AppContext, tmp_path: Path) -> None:
    result = note_save(
        tool_ctx(ctx),
        kind="card",
        cards=[
            {"front": "RAG 解决什么问题", "back": "知识更新与幻觉"},
            {"front": "检索器怎么训练", "back": "对比学习为主，见 src_aaa"},
        ],
        refs={"source_ids": ["src_aaa"]},
        tags=["检索"],
    )
    data = result["data"]
    assert data["card_count"] == 2
    assert data["path"].startswith("data/notes/")
    assert result["refs"]["source_ids"] == ["src_aaa"]

    target = paths.home() / data["path"]
    assert target.is_file()
    body = target.read_text(encoding="utf-8")
    assert "## Q1. RAG 解决什么问题" in body
    assert "## Q2. 检索器怎么训练" in body
    assert "tags: 检索" in body

    note = get_note(ctx.db, data["note_id"])
    assert note is not None
    assert note["kind"] == "card"
    assert note["path"] == data["path"]
    assert notes_for_source(ctx.db, "src_aaa")[0]["id"] == data["note_id"]


def test_save_single_card_from_title_and_content(ctx: AppContext) -> None:
    result = note_save(
        tool_ctx(ctx), kind="card", title="教育智能体是什么", content="先检索再作答的智能体"
    )
    assert result["data"]["card_count"] == 1
    note = get_note(ctx.db, result["data"]["note_id"])
    assert note is not None and "先检索再作答的智能体" in str(note["content"])


def test_save_note_and_summary_kinds(ctx: AppContext) -> None:
    note = note_save(tool_ctx(ctx), kind="note", content="随手记：先跑通再优化", tags=["随手"])
    summary = note_save(
        tool_ctx(ctx), kind="summary", title="本周小结", content="读了 3 篇，跑通 1 个 demo"
    )

    note_row = get_note(ctx.db, note["data"]["note_id"])
    summary_row = get_note(ctx.db, summary["data"]["note_id"])
    assert note_row is not None and note_row["kind"] == "note"
    assert summary_row is not None and summary_row["kind"] == "summary"
    assert summary_row["title"] == "本周小结"


def test_card_without_payload_is_rejected(ctx: AppContext) -> None:
    with pytest.raises(ValueError, match="kind=card 需要 cards"):
        note_save(tool_ctx(ctx), kind="card")


def test_card_with_missing_side_is_rejected(ctx: AppContext) -> None:
    with pytest.raises(ValueError, match="每张卡片都要有 front 与 back"):
        note_save(tool_ctx(ctx), kind="card", cards=[{"front": "只有问题", "back": "  "}])


def test_note_without_content_is_rejected(ctx: AppContext) -> None:
    with pytest.raises(ValueError, match="kind=note 需要 content"):
        note_save(tool_ctx(ctx), kind="note")

    with pytest.raises(ValueError, match="非法 kind"):
        note_save(tool_ctx(ctx), kind="日记", content="x")

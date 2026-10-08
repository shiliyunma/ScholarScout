"""③ 抽背卡的模型部分：「求助」与「延伸」的组装口径。

用桩模型（不联网、不花钱），但**断言的是提示词里真的带了该带的料** —— 用户要的是
"结合学习记录和知识库"，如果提示词里没带复习记录或资料 id，功能看着能跑、其实没做到。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scholarscout.runtime import practice
from scholarscout.state import learning


@dataclass
class StubReply:
    content: str
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt_tokens": 10, "completion_tokens": 5}
    )
    truncated: bool = False
    finish_reason: str = "stop"


class StubLlm:
    """记录最后一次提示词，方便断言"料到底带没带"。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.messages: list[dict[str, Any]] = []

    def chat(self, messages, *, tools=None):  # type: ignore[no-untyped-def]
        self.messages = messages
        return StubReply(content=self.content)

    @property
    def last_prompt(self) -> str:
        return str(self.messages[0]["content"]) if self.messages else ""


def _card_note(db) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    content = "## Q1. [填空] 中文检索用的 FTS5 分词器是？\n答案：trigram"
    note_id = learning.save_note(db, kind="card", content=content, title="卡组")
    return note_id, content


def test_help_prompt_carries_history_and_learned_sources(db) -> None:  # type: ignore[no-untyped-def]
    note_id, _ = _card_note(db)
    card = learning.parse_cards(learning.get_note(db, note_id)["content"])[0]
    card = {**card, "note_id": note_id, "card_index": 1}
    llm = StubLlm("分词器是 trigram。")

    outcome = practice.build_help(llm, db, session_id=None, card=card, response="不知道")

    prompt = llm.last_prompt
    assert "trigram" in prompt  # 卡片带了答案
    assert "不知道" in prompt  # 学生的作答带进去了
    assert "复习记录" in prompt  # 学习记录这一栏必须在
    assert "没有入库资料" in prompt  # 没有资料时如实说明，不留空
    assert outcome["text"] == "分词器是 trigram。"


def test_extension_parses_questions_and_stores_a_note(db) -> None:  # type: ignore[no-untyped-def]
    note_id, _ = _card_note(db)
    card = {
        **learning.parse_cards(learning.get_note(db, note_id)["content"])[0],
        "note_id": note_id,
        "card_index": 1,
    }
    llm = StubLlm(
        "## Q1. [选择] trigram 与 bigram 的主要区别？\nB. 切分粒度不同\n\n"
        "## Q2. [简答] 为什么 trigram 适合中文？\n因为中文没有词边界"
    )

    outcome = practice.build_extension(llm, db, session_id=None, card=card, response="不会")

    assert [item["type"] for item in outcome["questions"]] == ["choice", "short"]
    assert outcome["stored"] is True and outcome["note_id"]
    stored = learning.get_note(db, str(outcome["note_id"]))
    assert stored is not None and "[选择]" in stored["content"]
    assert "derived" in str(stored["tags"])  # get_note 返回的键名是 tags


def test_extension_does_not_store_garbage(db) -> None:  # type: ignore[no-untyped-def]
    """模型没按格式输出时：如实返回空题、**不建空笔记**（避免库里多出一堆废卡）。"""
    note_id, _ = _card_note(db)
    card = {
        **learning.parse_cards(learning.get_note(db, note_id)["content"])[0],
        "note_id": note_id,
        "card_index": 1,
    }
    llm = StubLlm("好的，我来出两道题：……（但没按格式写）")

    outcome = practice.build_extension(llm, db, session_id=None, card=card, response="不会")

    assert outcome["questions"] == [] and outcome["stored"] is False and outcome["note_id"] is None


def test_help_has_two_stages_and_the_hint_does_not_give_the_answer() -> None:
    """大浩选定「分两步」：先思路提示（不给答案），再点一次才完整讲解。

    这条断言的是**提示词确实分了两套**，而不是只看返回值长得不一样 ——
    hint 档必须明确要求模型不要说出答案，否则"两步"就等于把答案先说一遍。
    """
    card = {"type": "choice", "front": "题干", "answer": "B", "back": "B. 对的"}
    hint = practice.help_prompt(card, "不知道", "（无）", [], stage="hint")
    full = practice.help_prompt(card, "不知道", "（无）", [], stage="full")
    assert "不要给答案" in hint and "详细讲解" in hint
    assert hint != full
    assert "逐个说清其它选项错在哪" in full
    assert "复习记录" in hint and "复习记录" in full

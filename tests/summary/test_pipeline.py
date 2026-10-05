"""摘要流水线契约：体裁选择、解析、守卫、降级路径（模型用桩）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout.providers.llm import ChatReply, LlmError
from scholarscout.summary.pipeline import (
    SummaryInput,
    choose_kind,
    parse_output,
    summarize,
)
from scholarscout.summary.settings import SummarySettings

from ..conftest import StubLlm

REPO_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
SETTINGS = SummarySettings(prompts_dir=REPO_PROMPTS)

BODY = (
    "We propose an education agent that retrieves materials before answering. "
    "On the MATH dataset the reported accuracy rises from 42% to 58%. "
    "The code is released under an open license."
) * 6

GOOD_REPLY = (
    "title_zh: 教育智能体的检索增强框架\n"
    "summary_short: 该工作提出一个先检索资料再作答的教育智能体框架，并在 MATH 数据集上做了对比实验。"
    "报告称准确率从 42% 提升到 58%，代码以开源许可证发布，便于直接复现。\n"
    "points:\n"
    "- 在 MATH 上准确率由 42% 提升到 58%\n"
    "- 代码开源\n"
)


class FailingLlm:
    def __init__(self, kind: str = "timeout") -> None:
        self.kind = kind

    def chat(self, messages, *, tools=None):
        raise LlmError(self.kind, "桩模型失败")


def test_choose_kind_maps_sources() -> None:
    assert choose_kind("arxiv") == "paper"
    assert choose_kind("openalex") == "paper"
    assert choose_kind("github") == "repo"
    assert choose_kind("web") == "doc"
    assert choose_kind("arxiv", "no_text_layer") == "scanned"
    assert choose_kind("web", "mineru_failed") == "scanned"


def test_parse_output_with_labels() -> None:
    parsed = parse_output(GOOD_REPLY)
    assert parsed.labelled is True
    assert parsed.title_zh == "教育智能体的检索增强框架"
    assert parsed.summary_short.startswith("该工作提出")
    assert parsed.points == ("在 MATH 上准确率由 42% 提升到 58%", "代码开源")


def test_parse_output_without_labels_falls_back() -> None:
    parsed = parse_output("教育智能体摘要\n这是一段没有标签的回答。")
    assert parsed.labelled is False
    assert parsed.title_zh == "教育智能体摘要"
    assert "没有标签" in parsed.summary_short


def test_parse_output_empty() -> None:
    parsed = parse_output("")
    assert parsed.title_zh == "" and parsed.summary_short == ""


def test_summarize_happy_path() -> None:
    llm = StubLlm(ChatReply(content=GOOD_REPLY, model="stub-model"))
    draft = summarize(
        llm,
        SummaryInput(title="A", body=BODY, source_type="arxiv", abstract="abs"),
        settings=SETTINGS,
    )
    assert draft.degraded is False
    assert draft.kind == "paper"
    assert draft.title_zh == "教育智能体的检索增强框架"
    assert len(draft.points) == 2
    assert draft.guard_failed is False
    assert draft.ungrounded == ()
    assert len(draft.prompt_version) == 12
    assert draft.model == "stub-model"


def test_summarize_prompt_has_no_placeholders_left() -> None:
    llm = StubLlm(ChatReply(content=GOOD_REPLY))
    summarize(llm, SummaryInput(title="A", body=BODY, source_type="arxiv"), settings=SETTINGS)
    prompt = str(llm.calls[0]["messages"][0]["content"])
    assert "{{" not in prompt
    assert "反幻觉规则" in prompt


def test_summarize_truncates_long_body() -> None:
    settings = SummarySettings(prompts_dir=REPO_PROMPTS, truncate_chars=500)
    llm = StubLlm(ChatReply(content=GOOD_REPLY))
    draft = summarize(
        llm, SummaryInput(title="A", body=BODY, source_type="arxiv"), settings=settings
    )
    assert draft.input_truncated is True
    prompt = str(llm.calls[0]["messages"][0]["content"])
    assert "…" in prompt, "截断后应带省略号"
    assert prompt.count("We propose") < BODY.count("We propose"), "送进模型的正文必须是截断过的"


def test_summarize_flags_ungrounded_numbers() -> None:
    fabricated = GOOD_REPLY.replace("42%", "99%")
    llm = StubLlm(ChatReply(content=fabricated))
    draft = summarize(
        llm, SummaryInput(title="A", body=BODY, source_type="arxiv"), settings=SETTINGS
    )
    assert draft.guard_failed is True
    assert "99%" in draft.ungrounded


def test_summarize_compacts_overlong_reply() -> None:
    long_line = "该工作提出一种教育智能体框架，在 MATH 数据集上把准确率从 42% 提升到 58%。" * 5
    llm = StubLlm(ChatReply(content=f"title_zh: 长摘要\nsummary_short: {long_line}\n"))
    draft = summarize(
        llm, SummaryInput(title="A", body=BODY, source_type="arxiv"), settings=SETTINGS
    )
    assert len(draft.summary_short) <= SETTINGS.summary_max_chars
    assert "42%" in draft.summary_short


def test_summarize_falls_back_to_abstract_when_body_too_short() -> None:
    llm = StubLlm(ChatReply(content=GOOD_REPLY))
    summarize(
        llm,
        SummaryInput(
            title="A",
            body="BODYMARKER_SHORT",
            source_type="web",
            abstract="这是原文摘要" * 20,
        ),
        settings=SETTINGS,
    )
    prompt = str(llm.calls[0]["messages"][0]["content"])
    assert "这是原文摘要" in prompt
    assert "BODYMARKER_SHORT" not in prompt


def test_summarize_degrades_when_model_fails() -> None:
    draft = summarize(
        FailingLlm("timeout"),
        SummaryInput(title="标题", body=BODY, source_type="arxiv", abstract="这是原文摘要"),
        settings=SETTINGS,
    )
    assert draft.degraded is True
    assert "timeout" in str(draft.error)
    assert draft.summary_short == "这是原文摘要"


def test_summarize_no_content_returns_degraded_draft_without_calling_model() -> None:
    class Boom:
        def chat(self, messages, *, tools=None):
            raise AssertionError("没有正文时不应调用模型")

    draft = summarize(Boom(), SummaryInput(title="只有标题", body=""), settings=SETTINGS)
    assert draft.degraded is True
    assert draft.error == "no_content"
    assert draft.summary_short == "只有标题"


def test_scanned_kind_uses_doc_template() -> None:
    llm = StubLlm(ChatReply(content=GOOD_REPLY))
    draft = summarize(
        llm,
        SummaryInput(
            title="扫描件", body=BODY, source_type="arxiv", extract_status="no_text_layer"
        ),
        settings=SETTINGS,
    )
    assert draft.kind == "scanned"
    assert "文档类型" in str(llm.calls[0]["messages"][0]["content"])


@pytest.mark.parametrize("source_type", ["github", "web"])
def test_other_kinds_compose(source_type: str) -> None:
    llm = StubLlm(ChatReply(content=GOOD_REPLY))
    draft = summarize(
        llm, SummaryInput(title="t", body=BODY, source_type=source_type), settings=SETTINGS
    )
    assert draft.prompt_version != ""
    assert draft.summary_short

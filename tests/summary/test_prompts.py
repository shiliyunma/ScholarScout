"""提示词装载器契约：片段拼装、变量替换、版本=内容哈希、早失败。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout.summary.pipeline import SummaryInput, prompt_name_for, variables_for
from scholarscout.summary.prompts import PromptError, PromptPack

REPO_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
PROMPT_NAMES = ("summarize-paper", "summarize-doc", "summarize-repo")
#: 体裁 → 提示词文件（与 pipeline.PROMPT_BY_KIND 对应）
KIND_TO_PROMPT = {
    "paper": "summarize-paper",
    "doc": "summarize-doc",
    "repo": "summarize-repo",
    "scanned": "summarize-doc",
}


def _write(directory: Path, name: str, content: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(content, encoding="utf-8")


def test_include_and_variable_substitution(tmp_path: Path) -> None:
    _write(tmp_path, "rules-x", "规则：不要说套话")
    _write(tmp_path, "main", "标题：{{title}}\n{{> rules-x}}\n正文：{{body}}")
    pack = PromptPack(tmp_path)
    text = pack.compose("main", title="T", body="B")
    assert text == "标题：T\n规则：不要说套话\n正文：B"


def test_missing_variable_fails_loudly(tmp_path: Path) -> None:
    _write(tmp_path, "main", "标题：{{title}} 作者：{{authors}}")
    with pytest.raises(PromptError, match="authors"):
        PromptPack(tmp_path).compose("main", title="T")


def test_unknown_prompt_file_raises(tmp_path: Path) -> None:
    with pytest.raises(PromptError, match="不存在"):
        PromptPack(tmp_path).raw("nope")


def test_cyclic_include_raises(tmp_path: Path) -> None:
    _write(tmp_path, "a", "{{> b}}")
    _write(tmp_path, "b", "{{> a}}")
    with pytest.raises(PromptError, match="嵌套"):
        PromptPack(tmp_path).compose("a")


def test_version_changes_when_included_partial_changes(tmp_path: Path) -> None:
    _write(tmp_path, "rules-x", "规则 v1")
    _write(tmp_path, "main", "{{> rules-x}}")
    pack = PromptPack(tmp_path)
    first = pack.version_of("main")
    _write(tmp_path, "rules-x", "规则 v2")
    second = PromptPack(tmp_path).version_of("main")
    assert first != second
    assert len(first) == 12


def test_version_is_stable_across_variable_values(tmp_path: Path) -> None:
    _write(tmp_path, "main", "标题：{{title}}")
    pack = PromptPack(tmp_path)
    assert pack.version_of("main") == pack.version_of("main")


def test_available_lists_prompt_files(tmp_path: Path) -> None:
    _write(tmp_path, "b", "x")
    _write(tmp_path, "a", "x")
    assert PromptPack(tmp_path).available() == ["a", "b"]
    assert PromptPack(tmp_path / "missing").available() == []


@pytest.mark.parametrize("kind", sorted(KIND_TO_PROMPT))
def test_real_prompt_packs_compose_without_leftovers(kind: str) -> None:
    """真实提示词必须能被四种体裁拼出来，且不残留任何 ``{{``（模板写错会在这里炸）。"""
    pack = PromptPack(REPO_PROMPTS)
    prompt_name = KIND_TO_PROMPT[kind]
    assert prompt_name == prompt_name_for(kind), "映射必须与 pipeline 保持一致"
    item = SummaryInput(title="标题", source_type="arxiv", source_name="arXiv")
    variables = {**variables_for(kind, item), "body": "这里是正文"}
    text = pack.compose(prompt_name, **variables)
    assert "{{" not in text and "}}" not in text
    assert "这里是正文" in text
    assert pack.version_of(prompt_name) != ""


def test_real_prompt_packs_include_the_three_rule_partials() -> None:
    """反幻觉与答案前置规则必须出现在每个体裁的提示词里（照 AIHOT 的做法）。"""
    pack = PromptPack(REPO_PROMPTS)
    for prompt_name in PROMPT_NAMES:
        kind = next(k for k, v in KIND_TO_PROMPT.items() if v == prompt_name and k != "scanned")
        text = pack.compose(
            prompt_name,
            **{**variables_for(kind, SummaryInput(title="t")), "body": "b"},
        )
        assert "反幻觉规则" in text
        assert "答案前置" in text
        assert "学术事实保留规则" in text


def test_version_of_partials_is_different_from_body() -> None:
    pack = PromptPack(REPO_PROMPTS)
    assert pack.version_of("summarize-paper") != pack.version_of("summarize-doc")

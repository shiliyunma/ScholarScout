"""人格/风格档（D4）的口径测试。

最关键的一条：**默认档必须逐字节等于没这功能时的提示词** —— 否则实验臂被改了，
E3 那批数字就不可比，而"改实验臂"必须是有意识的行为，不能顺手发生。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout.config import STYLE_CHOICES, load_config, normalize_style
from scholarscout.runtime.prompts import (
    STYLE_PROMPTS,
    build_system_prompt,
    system_prompt_hash,
)
from scholarscout.settings_io import FIELDS, SettingsError

#: 与 tests/runtime/test_prompt_arms.py 里的钉值一致。
#: 历史值（2026-10-22 加 `py.exec` 的 `PY_EXEC_RULE` 那一版）：`b9ac0c043bf2` / `bd2b946b4e5d`。
#: 2026-10-22 再更新：**新增了 `skill.list` / `skill.load` 两个模型可见工具**（工具集 9 → 11），
#: 提示词加了 `SKILL_RULE`（只说"什么时候翻本地技能库"，清单按需取）—— 同样是**有意识的契约变更**，
#: 不是为凑绿改值；理由写在 `test_prompt_arms.py` 的钉值注释里。
#: 2026-10-25 再更新（`20b7b97e452e` / `c21d781d15d5` → 现值）：只因为两档共用位置多了一条
#: `SEARCH_PACE_RULE`（检索节制）。默认风格仍然**不加任何字符**，这条用例的语义没变。
GOLDEN_ON_DEMAND_HASH = "07325099d9ac"
GOLDEN_AUTO_HASH = "14ea4d9dd806"


def test_style_choices_and_prompt_texts_do_not_drift() -> None:
    """取值清单在 config 层、提示词文本在 runtime 层 —— 两处必须一一对应。

    加了一个档位却忘了写提示词（或反过来），用户改了设置却什么都没发生。
    """
    assert set(STYLE_CHOICES) == set(STYLE_PROMPTS)
    assert STYLE_PROMPTS["default"] == ""


def test_default_style_keeps_the_prompt_byte_identical() -> None:
    """默认档不加任何字符 —— 哈希与 E3 那批数字用的还是同一个提示词。"""
    assert system_prompt_hash(build_system_prompt()) == GOLDEN_ON_DEMAND_HASH
    assert system_prompt_hash(build_system_prompt(auto_artifacts=True)) == GOLDEN_AUTO_HASH
    assert build_system_prompt(style="default") == build_system_prompt()


@pytest.mark.parametrize(
    ("style", "marker"),
    [
        ("academic", "说话风格：严谨学术"),
        ("tutor", "说话风格：亲切助教"),
        ("concise", "说话风格：简洁"),
    ],
)
def test_each_style_adds_its_own_block(style: str, marker: str) -> None:
    prompt = build_system_prompt(style=style)
    assert marker in prompt
    # 只加自己那一段：不能把别的档位的话也带上
    others = [key for key in STYLE_PROMPTS if key not in {"default", style}]
    assert not any(STYLE_PROMPTS[key] in prompt for key in others if STYLE_PROMPTS[key])
    assert system_prompt_hash(prompt) != GOLDEN_ON_DEMAND_HASH


def test_unknown_style_fails_loudly() -> None:
    """写错的风格档必须报错。

    静默回默认档会变成"用户以为改了、其实没改" —— 这正是本项目在设置面板里
    明令禁止的那类失败（见 docs/01 §3.7 三条硬约定）。
    """
    with pytest.raises(ValueError):
        normalize_style("funny")
    with pytest.raises(ValueError):
        build_system_prompt(style="funny")


def test_style_is_read_from_yaml_and_validated(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("generation:\n  style: tutor\n", encoding="utf-8")
    assert load_config(path, load_env=False).generation.style == "tutor"

    path.write_text("generation:\n  style: 严谨一点\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path, load_env=False)


def test_style_is_in_the_settings_whitelist_with_choices() -> None:
    field = FIELDS["generation.style"]
    assert field.choices == STYLE_CHOICES
    assert field.check("tutor") == "tutor"
    with pytest.raises(SettingsError):
        field.check("严谨一点")


def test_blank_style_falls_back_to_default() -> None:
    """配置里写了空值／缺省 → 默认档（只有**写错**才报错，没写不算错）。"""
    assert normalize_style(None) == "default"
    assert normalize_style("") == "default"
    assert normalize_style("  TUTOR ") == "tutor"

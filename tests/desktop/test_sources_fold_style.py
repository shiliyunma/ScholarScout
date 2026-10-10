"""资料库列表「分组折叠」与组头外观的样式守卫。

这两条规则都踩过同一个坑：**改了、跑了门禁、看着绿，但界面上没生效**。
门禁只验"资源齐全 + 只调真实 RPC"，解析不了 CSS 的层叠与令牌，所以在这里钉住。

- 折叠：`.source-row.is-folded-row { display: none }` 的权重（0,2,0）压不过基础规则
  `#source-list li.source-row { display: flex }` 的 id 权重（1,1,1）→ class 加上了、行照样显示。
- 组头：写了 `var(--border)` / `var(--muted-foreground)` 等**这套设计里从未定义**的旧令牌名
  → 整条声明在"计算值阶段"失效（`border: 1px solid var(--border)` 连 `button` 基类的描边
  一起被抹掉，组头退化成裸文字）。
"""

from __future__ import annotations

import re

from ._ui import STYLE_CSS

#: 这套设计令牌体系是 --ui-* 家族（--ui-stroke-* / --ui-text-* / --radius-* …）。
#: 下面这些名字在 style.css 里**没有任何定义**，写进声明里等于把该声明废掉。
LEGACY_TOKENS = ("--border", "--foreground", "--muted-foreground", "--accent", "--background")

#: 资料库列表这一块的规则（只查这些，避免把全文件的存量问题算进来）。
SOURCES_SELECTOR = re.compile(r"(group-toggle|source-group|source-row|row-title|row-meta)")


def _rules(css: str) -> list[tuple[str, str]]:
    """粗切「选择器 → 声明块」；先剥注释，否则注释里的花括号会把规则切歪。"""
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", stripped):
        sel = " ".join(selector.split())
        if not sel:
            continue
        out.append((sel, " ".join(body.split())))
    return out


def test_base_row_rule_is_id_scoped() -> None:
    """前提：基础行样式是 id 选择器带 display:flex —— 所以折叠规则必须比它更具体。"""
    rules = _rules(STYLE_CSS)
    base = [sel for sel, body in rules if "source-row" in sel and "display: flex" in body]
    assert any(sel.startswith("#source-list") for sel in base), (
        f"没找到 #source-list li.source-row 的 display:flex（现有：{base}）—— 折叠规则的前提变了，请重新评估权重"
    )


def test_fold_rule_outweighs_the_flex_base() -> None:
    """折叠规则必须带 #source-list 前缀（权重 1,2,1 > 1,1,1），否则点了没反应。"""
    fold_rules = [(sel, body) for sel, body in _rules(STYLE_CSS) if "is-folded-row" in sel]
    assert fold_rules, "样式里找不到任何 is-folded-row 规则"
    for sel, body in fold_rules:
        assert sel.startswith("#source-list"), (
            f"折叠规则选择器 {sel!r} 没带 #source-list —— 权重压不过 "
            "#source-list li.source-row{display:flex}，class 加了也不会隐藏"
        )
        assert "display: none" in body, f"折叠规则 {sel!r} 没有 display:none"


def test_sources_block_avoids_undefined_legacy_tokens() -> None:
    """资料库这一块不许再用未定义的旧令牌名（它们会让整条声明静默失效）。"""
    offenders = []
    for sel, body in _rules(STYLE_CSS):
        if not SOURCES_SELECTOR.search(sel):
            continue
        for token in LEGACY_TOKENS:
            if f"var({token})" in body:
                offenders.append((sel, token))
    assert not offenders, (
        f"资料库样式用了未定义的旧令牌 {offenders} —— "
        "改用 --ui-stroke-secondary / --ui-text-secondary / --ui-control-hover-background 等 --ui-* 令牌"
    )

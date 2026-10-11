"""左侧栏「收起」样式的守卫。

这些规则全都踩过同一个坑：**改了、跑了门禁、看着绿，但界面上没生效**。
门禁只验"资源齐全 + 只调真实 RPC"，解析不了 CSS 的层叠、令牌与 grid 流，所以在这里钉住：

- **权重**：收起态轨道规则 `.layout.is-side-collapsed { grid-template-columns: … }`（两个类）
  必须压过基础 `.layout { … }`（一个类）。基础列的宽窄是 JS 写在 `--col-left/--col-right`
  内联样式上的，但轨道本身来自 `.layout` 类规则 —— 只写一个类 `.is-side-collapsed` 与基础同分，
  靠源序未必赢，收起就"点了没反应"。
- **收起态分隔条不能用 display:none**：`#split-left` 一旦 `display:none` 就**退出 grid 流**，
  后面的列整体前移一格 —— `col-chat` 掉进 0px 轨道被挤成 1px、`col-right` 掉进 6px 分隔条轨道。
  这是**真机渲染核对抓到的真 bug**（截图里中栏内容被挤成竖排、右栏页签竖排）。必须用
  `visibility:hidden`（保留盒子占位、只是不显示）。
- **令牌**：不许用这套设计里从未定义的旧令牌名（`var(--border)` 之类）—— 整条声明会静态失效。
"""

from __future__ import annotations

import re

from ._ui import STYLE_CSS

#: 这套设计令牌是 --ui-* 家族（--ui-stroke-* / --ui-text-* / --radius-* …）。
#: 下面这些名字在 style.css 里**没有任何定义**，写进声明里等于把该声明废掉。
LEGACY_TOKENS = ("--border", "--foreground", "--muted-foreground", "--accent", "--background")

#: 只查这次改动引入的那几块（避免把全文件的存量问题算进来）。
SIDEBAR_SELECTOR = re.compile(r"(is-side-collapsed|side-toggle|col-head-actions)")


def _rules(css: str) -> list[tuple[str, str]]:
    """粗切「选择器 → 声明块」；先剥注释，否则注释里的花括号/字样会把规则切歪、测假。"""
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", stripped):
        sel = " ".join(selector.split())
        if not sel:
            continue
        out.append((sel, " ".join(body.split())))
    return out


def test_base_layout_declares_the_tracks() -> None:
    """前提：基础轨道写在 `.layout` 这个**单类**选择器上 —— 所以收起态规则必须比它更具体。"""
    base = [
        sel
        for sel, body in _rules(STYLE_CSS)
        if sel == ".layout" and "grid-template-columns" in body
    ]
    assert base, "找不到 .layout 的基础网格轨道定义 —— 收起态规则的前提变了，请重新评估权重"


def test_collapsed_track_rule_outweighs_the_base_layout() -> None:
    """收起态轨道规则的选择器必须带 `.layout.`（两个类，权重压过基础的一个类）。"""
    collapsed = [
        (sel, body)
        for sel, body in _rules(STYLE_CSS)
        if "is-side-collapsed" in sel and "grid-template-columns" in body
    ]
    assert collapsed, "样式里找不到收起态（is-side-collapsed）的 grid-template-columns 规则"
    for sel, body in collapsed:
        assert sel == ".layout.is-side-collapsed", (
            f"收起态轨道规则选择器是 {sel!r} —— 必须写成 .layout.is-side-collapsed"
            "（两个类，权重 0,2,0 压过基础 .layout 的 0,1,0）；只写 .is-side-collapsed 与基础同分，收起会点了没反应"
        )
        assert "--side-rail-width" in body, "收起态左栏宽度应走 --side-rail-width（图标条宽度）"


def test_collapsed_splitter_is_hidden_without_leaving_grid_flow() -> None:
    """收起态藏左分隔条**只能用 visibility**：display:none 会让它退出 grid 流、列全错位。"""
    hidden = [
        (sel, body)
        for sel, body in _rules(STYLE_CSS)
        if "is-side-collapsed" in sel and "split-left" in sel
    ]
    assert hidden, "找不到收起态左分隔条（#split-left）的规则"
    for sel, body in hidden:
        assert "display: none" not in body, (
            f"{sel!r} 用 display:none 藏分隔条 —— 它会退出 grid 流，后面的列整体前移一格"
            "（col-chat 掉进 0px 轨道被挤成 1px、col-right 掉进 6px 轨道）。改用 visibility:hidden"
        )
        assert "visibility: hidden" in body, f"{sel!r} 没用 visibility:hidden"


def test_collapsed_rule_hides_title_new_button_and_list() -> None:
    """收起后只留图标条：标题 / ＋新建 / 会话列表都要让位。"""
    hidden = " ".join(
        sel
        for sel, body in _rules(STYLE_CSS)
        if "is-side-collapsed" in sel and "display: none" in body
    )
    assert hidden, "找不到任何隐藏规则"
    for target in ("#btn-new", "#session-list"):
        assert target in hidden, f"收起态没有隐藏 {target}"


def test_sidebar_block_avoids_undefined_tokens() -> None:
    """左栏收起这一块不许再用未定义的旧令牌名（它们会让整条声明静默失效）。"""
    offenders = []
    for sel, body in _rules(STYLE_CSS):
        if not SIDEBAR_SELECTOR.search(sel):
            continue
        for token in LEGACY_TOKENS:
            if f"var({token})" in body:
                offenders.append((sel, token))
    assert not offenders, (
        f"左栏收起样式用了未定义的旧令牌 {offenders} —— "
        "改用 --ui-stroke-secondary / --ui-text-secondary / --ui-control-hover-background 等 --ui-* 令牌"
    )

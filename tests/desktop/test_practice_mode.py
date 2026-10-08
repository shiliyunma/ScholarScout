"""抽背「练」模式的界面接线（漂移测试）。

界面这一层没法在 pytest 里跑，所以钉住"必须存在的东西"：
按钮、抽屉、以及它调用的 RPC —— 任何一个被重命名，用户就会点到一个死按钮。
"""

from __future__ import annotations

import re

from ._ui import APP_JS, INDEX_HTML_PATH, STYLE_CSS_PATH


def test_practice_markup_exists() -> None:
    html = INDEX_HTML_PATH.read_text(encoding="utf-8")
    for element_id in (
        "practice",
        "practice-body",
        "practice-progress",
        "btn-practice",
        "btn-practice-close",
        "btn-practice-reveal",
        "practice-stats",
    ):
        assert f'id="{element_id}"' in html, f"缺少元素 {element_id}"


def test_practice_calls_the_review_rpcs() -> None:
    text = APP_JS
    assert 'scout.call("note.review"' in text, "没有记评价的调用"
    assert 'scout.call("note.stats"' in text, "没有取抽背统计的调用"
    for rating in ("good", "fuzzy", "bad"):
        assert f'"{rating}"' in text, f"缺少评价档位 {rating}"


def test_practice_requires_revealing_before_rating() -> None:
    """没看答案不许自评 —— 否则会一路点"会了"，那数据就没有意义。"""
    text = APP_JS
    assert "button.disabled = !practice.revealed" in text
    assert "if (!state.practice.revealed) return;" in text, "键盘路径也要拦"


def test_practice_has_styles() -> None:
    css = STYLE_CSS_PATH.read_text(encoding="utf-8")
    for selector in (".practice-front", ".practice-back", ".practice-ratings"):
        assert selector in css, f"缺少样式 {selector}"


def test_all_element_ids_used_by_js_exist_in_html() -> None:
    """通用漂移：`el("x")` 用到的 id 必须在 HTML 里存在（否则运行时静默失效）。"""
    app = APP_JS
    html = INDEX_HTML_PATH.read_text(encoding="utf-8")
    used = set(re.findall(r'el\("([a-z0-9-]+)"\)', app))
    declared = set(re.findall(r'id="([a-z0-9-]+)"', html))
    # 动态生成的 id（设置面板按字段拼、分隔条按侧拼）不算
    dynamic = {"set-width-left", "set-width-right", "set-ui-scale"}
    missing = sorted(used - declared - dynamic)
    assert not missing, f"这些 id 在 HTML 里不存在：{missing}"

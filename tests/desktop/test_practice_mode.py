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


def test_practice_answers_in_an_input_box_not_by_self_rating() -> None:
    """用户要求：去掉「会/模糊/不会」自测，默认在输入区域作答。

    抽背的排期改由判分结果自动推导（autoReview），所以这里必须同时钉住：
    输入框在、自测按钮不在、note.review 仍被调用（排期没断）。
    """
    text = APP_JS  # _ui.py 里 APP_JS 已是字符串（B6 改的）
    assert "在输入框里作答" in text or "在这里作答" in text, "没有输入区"
    assert "rate-good" not in text and "rate-bad" not in text, "自测按钮还在"
    assert "autoReview" in text and 'scout.call("note.review"' in text, "排期没记上"
    assert "practice-jump" in text and "上一题" in text and "下一题" in text, "缺选题/翻页"


def test_practice_has_styles() -> None:
    css = STYLE_CSS_PATH.read_text(encoding="utf-8")
    for selector in (".practice-front", ".practice-back", ".practice-ratings"):
        assert selector in css, f"缺少样式 {selector}"


def test_practice_delta_is_appended_in_the_ui() -> None:
    """抽背流式：api.js 必须订阅 ``practice.delta`` 并把增量**追加**到提示区。

    还要钉住位置 —— 它必须在"按 session_id 找槽"那道门**之前**处理，
    否则抽背（不是 chat 轮次、没有会话槽）永远收不到增量。
    """
    text = APP_JS
    assert '"practice.delta"' in text, "api.js 没订阅 practice.delta"
    assert "practice-help-out" in text, "没找到抽背提示区"
    assert text.index('"practice.delta"') < text.index("const key = eventSessionKey("), (
        "practice.delta 必须放在按会话找槽这道门之前处理"
    )


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

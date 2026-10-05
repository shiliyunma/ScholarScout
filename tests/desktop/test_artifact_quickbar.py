"""产物快捷栏：把"按需生成"变成真按钮（用户要的是**选择的空间**，不是打字碰运气）。

要点：
- 三个按钮对应三件产物（知识地图 / 学习路线 / 抽背卡）；
- 点按钮发的是**一条真实的用户消息**（会话记录里看得见"我点过要什么"），不是偷偷调工具；
- 上一轮没跑完时不许叠加（并发两轮会把会话记录搅乱）。
"""

from __future__ import annotations

import re
from pathlib import Path

UTILS = Path(__file__).resolve().parents[2] / "src" / "scholarscout" / "desktop" / "ui"
APP_JS = (UTILS / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (UTILS / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (UTILS / "style.css").read_text(encoding="utf-8")


def test_quickbar_markup_has_three_actions() -> None:
    assert 'id="quickbar"' in INDEX_HTML
    for ask in ("map", "route", "cards"):
        assert f'data-ask="{ask}"' in INDEX_HTML, ask


def test_quickbar_says_it_will_not_auto_generate() -> None:
    """按钮旁的说明要让人知道"默认不会自动生成"，不然用户会以为产物没了。"""
    assert "默认不会自动生成" in INDEX_HTML


def test_each_action_asks_for_a_specific_tool() -> None:
    block = APP_JS[APP_JS.index("var ASK_TEXTS") : APP_JS.index("function askArtifact")]
    assert "topic.build_map" in block
    assert "route.plan" in block
    assert "note.save" in block and "card" in block


def test_asking_sends_a_real_user_message() -> None:
    """走 send(...)（真实对话），不是绕过会话直接调工具 —— 否则用户看不到自己点过什么。"""
    block = APP_JS[APP_JS.index("function askArtifact") : APP_JS.index("function wire()")]
    assert re.search(r"\bsend\(text\)", block)


def test_asking_is_blocked_while_a_turn_is_running() -> None:
    block = APP_JS[APP_JS.index("function askArtifact") : APP_JS.index("function wire()")]
    assert "state.sending" in block


def test_quickbar_is_styled() -> None:
    assert ".quickbar" in STYLE_CSS

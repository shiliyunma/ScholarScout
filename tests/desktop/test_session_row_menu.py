"""会话行三点菜单（置顶 / 重命名 / 分享 / 删除）的界面契约。

这块最容易出的错是**说一套做一套**：按钮写了名字、方法却不存在（点了没反应）；
或者删除的确认话术没告诉用户会连带删掉什么。所以这里把"话术"也钉住。
"""

from __future__ import annotations

from pathlib import Path

UTILS = Path(__file__).resolve().parents[2] / "src" / "scholarscout" / "desktop" / "ui"
APP_JS = (UTILS / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (UTILS / "index.html").read_text(encoding="utf-8")


def test_row_menu_has_the_four_actions() -> None:
    block = APP_JS[APP_JS.index("function toggleRowMenu") : APP_JS.index("function closeRowMenus")]
    for label in ("置顶", "取消置顶", "重命名", "分享（导出 Markdown）", "删除会话"):
        assert label in block, f"菜单里少了「{label}」"


def test_row_menu_button_sits_in_the_session_row() -> None:
    assert "rowMenuButton(item, li)" in APP_JS
    assert 'button.textContent = "⋯"' in APP_JS


def test_row_menu_calls_only_real_methods() -> None:
    for method in ("session.pin", "session.delete", "session.export", "session.rename"):
        assert f'"{method}"' in APP_JS, method


def test_delete_confirmation_names_what_gets_deleted() -> None:
    """删除是不可逆的：确认话术必须说清"连带删什么、不删什么"。"""
    block = APP_JS[
        APP_JS.index("async function deleteSession") : APP_JS.index("function showTextModal")
    ]
    assert "window.confirm" in block
    assert "资料库" in block and "账本" in block
    assert "分享" in block, "要提示删之前可以导出"


def test_delete_reports_what_was_removed() -> None:
    block = APP_JS[
        APP_JS.index("async function deleteSession") : APP_JS.index("function showTextModal")
    ]
    assert "result.removed" in block and "条消息" in block


def test_export_modal_is_readonly_and_copyable() -> None:
    assert 'id="textmodal-body" readonly' in INDEX_HTML
    assert 'id="btn-textmodal-copy"' in INDEX_HTML
    block = APP_JS[APP_JS.index("async function copyTextModal") : APP_JS.index("function wire()")]
    assert "navigator.clipboard" in block
    assert 'execCommand("copy")' in block, "没有剪贴板权限时要退回到选中复制"
    assert "area.select()" in block


def test_export_shows_where_the_file_went() -> None:
    assert 'id="textmodal-path"' in INDEX_HTML
    block = APP_JS[APP_JS.index("function showTextModal") : APP_JS.index("function closeTextModal")]
    assert "已保存：" in block


def test_menu_click_does_not_also_open_the_session() -> None:
    """点三个点不该顺手把会话切换掉（stopPropagation 漏了就是这个症状）。"""
    block = APP_JS[APP_JS.index("function rowMenuButton") : APP_JS.index("function toggleRowMenu")]
    assert "stopPropagation" in block


def test_thread_menu_closes_on_outside_click() -> None:
    assert 'document.addEventListener("click", closeRowMenus' in APP_JS


def test_messages_bump_only_via_real_ordering_field() -> None:
    """置顶必须在后端排（pinned DESC），前端不许自己插队 —— 否则刷新就乱。"""
    from scholarscout.state import sessions

    source = Path(sessions.__file__).read_text(encoding="utf-8")
    assert "COALESCE(s.pinned, 0) DESC" in source
    assert "item.pinned" in APP_JS, "界面要能看出哪条是置顶的"
    assert "pin-flag" in APP_JS and ".pin-flag" in (UTILS / "style.css").read_text(encoding="utf-8")

"""设置面板与后端白名单**不能漂移**。

面板是由 app.js 里的 `SETTINGS_SPEC` 画出来的，字段名直接当 RPC 参数用；
后端 `settings_io.FIELDS` 会拒掉不认识的键 —— 一旦两边对不上，
用户点"保存"就会看到"不认识的设置项：llm.chat.modle"，
而这类拼写错误在浏览器里**不会报错**，只有真去保存才暴露。

所以在这里把两边钉一起：改了一边忘了另一边，测试就红。
"""

from __future__ import annotations

import re
from pathlib import Path

UTILS = Path(__file__).resolve().parents[2] / "src" / "scholarscout" / "desktop" / "ui"
APP_JS = UTILS / "app.js"
INDEX_HTML = UTILS / "index.html"


def read_app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def spec_keys() -> list[str]:
    """从 SETTINGS_SPEC 里抽出所有 `key: "a.b.c"`（只取设置面板那一段）。"""
    text = read_app_js()
    start = text.index("var SETTINGS_SPEC = [")
    end = text.index("var THEME_CHOICES")
    return re.findall(r'key:\s*"([a-z0-9_.]+)"', text[start:end])


def test_panel_fields_exist_in_backend_whitelist() -> None:
    from scholarscout import settings_io

    known = set(settings_io.FIELDS) | set(settings_io.SECRET_FIELDS)
    unknown = [key for key in spec_keys() if key not in known]
    assert not unknown, f"面板里的这些字段后端不认（保存必然失败）：{unknown}"


def test_backend_fields_are_reachable_from_the_panel() -> None:
    """反向：后端能改的字段（除外观类）应该在面板里有入口，不然等于藏起来了。"""
    from scholarscout import settings_io

    keys = set(spec_keys())
    hidden = [
        key
        for key in settings_io.FIELDS
        if key not in keys
        and not key.startswith(("search.tavily", "search.serper", "search.brave"))
    ]
    assert not hidden, f"后端能改但面板没有入口：{hidden}"


def test_index_html_has_the_panel_markup() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    for marker in (
        'id="btn-settings"',
        'id="settings"',
        'id="settings-body"',
        'id="btn-settings-save"',
    ):
        assert marker in html, f"界面里少了 {marker}"


def test_panel_calls_only_settings_methods() -> None:
    text = read_app_js()
    methods = set(re.findall(r'scout\.call\(\s*"([a-zA-Z.]+)"', text))
    assert {"settings.get", "settings.update", "settings.usage", "settings.cleanup"} <= methods


def test_secret_inputs_are_never_prefilled() -> None:
    """密钥输入框只能是空密码框 —— 一旦有人给它塞 value，密钥就会出现在界面与截图里。"""
    text = read_app_js()
    block = text[
        text.index("function renderSettingsField") : text.index("function renderAppearance")
    ]
    assert 'input.type = "password"' in block
    secret_branch = block[block.index('field.kind === "secret"') : block.index("} else {")]
    assert "value" not in secret_branch, "密钥框不许预填任何值"


def test_bool_fields_are_checked_only_on_explicit_true() -> None:
    """前端布尔框只认明确的 true —— 值缺失时必须显示未勾选，否则"默认关"的开关看起来是开的。"""
    text = read_app_js()
    block = text[
        text.index("function renderSettingsField") : text.index("function renderAppearance")
    ]
    assert "input.checked = values[field.key] === true" in block


def test_ui_scale_is_clamped() -> None:
    """界面缩放要有上下限：放大到 300% 会把布局顶烂，缩到 30% 是看不见的字。"""
    text = read_app_js()
    assert "Math.min(1.25, Math.max(0.9" in text


def test_settings_overlay_is_not_open_at_startup() -> None:
    """面板默认关闭（否则一进界面就被糊住）。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert re.search(r'id="settings"[^>]*hidden', html)

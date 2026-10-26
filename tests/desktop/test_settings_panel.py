"""设置面板与后端白名单**不能漂移**。

面板是由 app.js 里的 `SETTINGS_SPEC` 画出来的，字段名直接当 RPC 参数用；
后端 `settings_io.FIELDS` 会拒掉不认识的键 —— 一旦两边对不上，
用户点"保存"就会看到"不认识的设置项：llm.chat.modle"，
而这类拼写错误在浏览器里**不会报错**，只有真去保存才暴露。

所以在这里把两边钉一起：改了一边忘了另一边，测试就红。
"""

from __future__ import annotations

import re

from ._ui import INDEX_HTML_PATH, UI_DIR, between, read_app_js


def spec_keys() -> list[str]:
    """从 SETTINGS_SPEC 里抽出所有 `key: "a.b.c"`（只取设置面板那一段）。

    结束锚点用**紧跟其后**的 `var SETTINGS_CATEGORIES`：之前用 `var THEME_CHOICES`，
    而它在 app.js 里排在 SETTINGS_SPEC **之前** → `between` 退化成"取 4000 字符"，
    于是 spec 末尾新增的那几组字段对这条守卫**隐形**（真实后果：`runtime.notify_turn_done`
    明明在面板里，却被判成"后端能改但面板没有入口"）。
    """
    text = read_app_js()
    block = between(text, "var SETTINGS_SPEC = [", "var SETTINGS_CATEGORIES = [")
    return re.findall(r'key:\s*"([a-z0-9_.]+)"', block)


def test_panel_fields_exist_in_backend_whitelist() -> None:
    from scholarscout import settings_io

    known = set(settings_io.FIELDS) | set(settings_io.SECRET_FIELDS)
    unknown = [key for key in spec_keys() if key not in known]
    assert not unknown, f"面板里的这些字段后端不认（保存必然失败）：{unknown}"


def test_backend_fields_are_reachable_from_the_panel() -> None:
    """反向：后端能改的字段（除外观类）应该在面板里有入口，不然等于藏起来了。"""
    from scholarscout import settings_io

    keys = set(spec_keys())
    hidden = [key for key in settings_io.FIELDS if key not in keys]
    assert not hidden, f"后端能改但面板没有入口：{hidden}"


def test_all_secret_fields_are_reachable_from_the_panel() -> None:
    """每个密钥字段都必须能在面板里填 —— 否则"添加 key 让来源可用"就是句空话。

    （真实缺口：Serper / Brave / Google CSE / Semantic Scholar 的 key 曾经只存在于后端
    白名单里，面板上没有入口，用户只能去手改 `.env`。）
    """
    from scholarscout import settings_io

    keys = set(spec_keys())
    missing = [key for key in settings_io.SECRET_FIELDS if key not in keys]
    assert not missing, f"这些密钥面板里填不了：{missing}"


def test_search_group_shows_provider_status() -> None:
    """搜索引擎那一组必须画状态表（用户要看到"哪些能用"），且必须有添加 key 的入口。"""
    text = read_app_js()
    assert "providers: true" in text, "搜索引擎组没有标记 providers，状态表不会被渲染"
    assert 'scout.call("search.providers"' in text, "没有调用 search.providers"
    for reason in ("missing_api_key", "missing_config", "blocked_by_robots"):
        assert reason in text, f"界面没有把 {reason} 翻成人话"


def test_data_group_has_maintenance_entries() -> None:
    """「数据」栏要给出缓存保留期与数据库整理入口 —— 且不能给"一键清空资料"的入口。

    整理数据库只整理不删数据（与"资料本体拒绝清理"这条硬约定一致），
    缓存则按保留期删（全清会让下次检索重新下载一遍）。
    """
    text = read_app_js()
    assert 'scout.call("settings.optimize"' in text, "没有整理数据库的入口"
    assert "older_than_days" in text, "缓存清理没有走保留期"
    assert "整理数据库" in text
    for forbidden in (
        'settings.cleanup", { target: "raw"',
        'settings.cleanup", { target: "database"',
    ):
        assert forbidden not in text, "界面不该提供清理资料本体/数据库的入口"


def test_mineru_without_token_explains_the_boundary() -> None:
    """ "开关开着 + 没配 token"要说明**能力边界**，而不是说"一定失败"。

    背景：2026-10-07 实测打通了**免 Token 的预签名直传**（本地扫描版样本 10.3 秒出正文），
    所以这里不能再写"没 token 就会失败"—— 那是过期结论。
    """
    text = read_app_js()
    assert "features.enable_mineru" in text and "mineru.api_token" in text
    assert "10MB" in text and "20 页" in text, "没写免 Token 通道的规模上限"
    assert "200MB" in text, "没写填 Token 后精准接口能处理更大文件"
    assert "mineru_failed" in text, "没有说明失败会被如实记成 mineru_failed"
    assert "model service is temporarily unavailable" not in text, "这条失败结论已经过期"


def test_index_html_has_the_panel_markup() -> None:
    html = INDEX_HTML_PATH.read_text(encoding="utf-8")
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
    block = between(text, "function renderSettingsField", "function renderAppearance")
    assert 'input.type = "password"' in block
    secret_branch = between(block, 'field.kind === "secret"', "} else {")
    assert "value" not in secret_branch, "密钥框不许预填任何值"


def test_bool_fields_are_checked_only_on_explicit_true() -> None:
    """前端布尔框只认明确的 true —— 值缺失时必须显示未勾选，否则"默认关"的开关看起来是开的。"""
    text = read_app_js()
    block = between(text, "function renderSettingsField", "function renderAppearance")
    assert "input.checked = values[field.key] === true" in block


def test_ui_scale_is_clamped() -> None:
    """界面缩放要有上下限：放大到 300% 会把布局顶烂，缩到 30% 是看不见的字。"""
    text = read_app_js()
    assert "Math.min(1.25, Math.max(0.9" in text


def test_settings_overlay_is_not_open_at_startup() -> None:
    """面板默认关闭（否则一进界面就被糊住）。"""
    html = INDEX_HTML_PATH.read_text(encoding="utf-8")
    assert re.search(r'id="settings"[^>]*hidden', html)


def test_download_switch_sits_in_its_own_feature_group() -> None:
    """「允许按需下载」要在「功能」分组里，且**说清后果**（关＝不出网）。

    用户反馈「设置→功能→允许下载这条链路并没有做好」：开关分散在「生成策略」里、
    文案也不提"会不会对外发请求"，用户不知道这个勾意味着什么。
    """
    block = between(read_app_js(), 'title: "功能"', 'title: "搜索引擎"')
    assert 'key: "features.enable_download"' in block, "「功能」组里没有这个开关"
    assert "允许按需下载" in block, "标签不是「允许按需下载」"
    assert "关：不会对外发请求" in block, "没写关掉的后果"
    assert "「获取正文」与资料库的「下载」才能用" in block, "没写打开的后果"
    assert '"功能"' in between(
        read_app_js(), "var SETTINGS_CATEGORIES = [", "var PROFILE_CATEGORIES = ["
    ), "「功能」组没挂进任何分类 —— 打开设置看不到它"


def test_open_settings_can_jump_to_a_field() -> None:
    """`openSettings(key)` 要真的定位：切到该字段所在分类 + 滚进视口 + 聚焦。

    地图里的「去设置」只打开面板是不够的（5 个分类、几十行），用户还得自己翻；
    面板这一侧必须能按字段名定位到行。
    """
    text = (UI_DIR / "profile.js").read_text(encoding="utf-8")
    assert "async function openSettings(focusKey)" in text
    body = between(text, "function revealSettingsField", "if (input.focus) input.focus();")
    assert "fieldId(key)" in body, "没按字段名找输入框"
    assert 'closest(".set-row")' in body, "没定位到行"
    assert 'pane.getAttribute("data-cat")' in body and "shell.select(cat)" in body, (
        "没切到该字段所在的分类（会停在上次那一栏）"
    )
    assert "scrollIntoView" in body, "没滚进视口"
    assert text.count("openSettings(focusKey)") == 1, "带字段名打开的分支没接上"

"""设置页「飞书接入」卡片的契约测试（静态漂移 + 无头真跑一遍渲染）。

分两层：
1. **无头渲染探针**（`feishu_card_probe.js`，用 node 真跑 `renderFeishuCard`）——
   卡片「只报布尔、绝不回显密钥、配对码生成前后行为、自检」这些**只有渲染一遍才看得到**的事实；
   探针往回包里**故意塞诱饵密钥**，断言渲染文本里找不到它们（静态读文本查不出这类泄漏）。
2. **静态漂移断言**——卡片接线（openSettings 调它）、只调 `feishu.*`（不把飞书键塞进
   `settings.update`，否则必被后端拒未知键）、文案（策略三选一 / 配对码倒计时）、样式。

为什么值得：设置栏这一层没法在 pytest 里进浏览器，改错了只会**静默失效**——
卡片不显示、点了没反应、或更糟：把密钥回显到页面里。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ._ui import APP_JS, STYLE_CSS_PATH, UI_DIR

PROFILE_JS = UI_DIR / "profile.js"
INDEX_HTML_PATH = UI_DIR / "index.html"
PROBE_JS = Path(__file__).resolve().parent / "feishu_card_probe.js"


# --------------------------------------------------------------- 无头渲染探针


@pytest.fixture(scope="module")
def card_render() -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("没装 node，跳过飞书卡片的无头渲染探针")
    assert PROFILE_JS.is_file(), f"缺少 profile.js：{PROFILE_JS}"
    completed = subprocess.run(
        [node, str(PROBE_JS), str(PROFILE_JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert completed.returncode == 0, f"node 跑挂了：{completed.stderr}"
    payload = (completed.stdout or "").strip()
    assert payload, "node 没有任何输出"
    return json.loads(payload)


def test_every_card_check_passes(card_render: dict[str, object]) -> None:
    checks = card_render["checks"]
    assert isinstance(checks, list) and checks, "探针没跑出任何断言"
    failures = [f"{c['name']} —— {c['got']}" for c in checks if not c.get("ok")]
    assert not failures, "飞书卡片渲染断言失败：\n" + "\n".join(failures)


def test_card_render_never_contains_any_secret_value(card_render: dict[str, object]) -> None:
    """真渲染出的文本里不能出现任何诱饵密钥 —— 凭据只能以「已配置／未配置」出现。"""
    text = str(card_render["text"])
    for leak in ("cli_LEAK_APPID", "SECRET_LEAK_DO_NOT_SHOW", "t-LEAKTOKEN"):
        assert leak not in text, f"渲染文本里泄漏了密钥值：{leak}"


def test_probe_covers_the_risky_areas(card_render: dict[str, object]) -> None:
    """用例集本身别缩水：这几个面必须都有覆盖（少一条就等于少一道防线）。"""
    names = " ".join(str(c["name"]) for c in card_render["checks"])
    for keyword in ("已配置", "密钥", "策略", "配对码", "机器人名", "token"):
        assert keyword in names, f"探针缺少对「{keyword}」的覆盖"
    assert len(card_render["checks"]) >= 10, f"断言太少（{len(card_render['checks'])} 条）"


# --------------------------------------------------------------- 静态漂移断言


def _card_block() -> str:
    text = PROFILE_JS.read_text(encoding="utf-8")
    start = text.index("async function renderFeishuCard(body)")
    end = text.index("function renderUsage", start)
    return text[start:end]


def test_card_is_wired_into_open_settings() -> None:
    """卡片必须被 openSettings 接进来，且落在**分类面板**里（不是甩在浮层根上）。

    本轮把设置浮层分了类：卡片容器从 `body` 变成 `panels.channel`（「渠道」那一类）。
    断言跟着改容器名，但**判得更严**：容器必须是分类面板之一的变量，不许是 `body`。
    """
    text = PROFILE_JS.read_text(encoding="utf-8")
    assert "async function renderFeishuCard(body)" in text, "卡片函数没了或改了签名"
    assert "await renderFeishuCard(panels.channel)" in text, "openSettings 没把卡片接进「渠道」分类"
    assert "await renderFeishuCard(body)" not in text, "卡片又挂回浮层根上（分类等于没做）"


def test_card_calls_only_feishu_methods() -> None:
    """卡片只准调 `feishu.*` —— 飞书键塞进 `settings.update` 会被后端拒（不认识这些键）。"""
    block = _card_block()
    methods = set(re.findall(r'scout\s*\.\s*call\(\s*"([^"]+)"', block))
    assert methods, "卡片没调任何后端方法"
    non_feishu = sorted(m for m in methods if not m.startswith("feishu."))
    assert not non_feishu, f"卡片调了非 feishu.* 的方法：{non_feishu}"
    for forbidden in ("settings.update", "settings.cleanup", "settings.optimize"):
        assert forbidden not in block, f"卡片不该出现 {forbidden}（不认识的键 / 不该有清理入口）"


def test_card_shows_credentials_as_booleans_only() -> None:
    """凭据只显示「已配置／未配置」：读的是 `*_set` 布尔，绝不触碰密钥字段本身。"""
    block = _card_block()
    assert '"已配置"' in block and '"未配置"' in block
    assert "app_id_set" in block and "secret_set" in block
    assert "app_secret" not in block, "卡片代码里出现了 app_secret —— 密钥不该被界面读到"
    assert "tenant_access_token" not in block, "卡片代码里出现了 token —— token 不该进界面"


def test_card_policy_and_pairing_copy() -> None:
    text = PROFILE_JS.read_text(encoding="utf-8")
    block = _card_block()
    # 策略文案与倒计时文案在卡片内的子函数/常量里，读整份 profile.js
    for label in ("配对审批（推荐）", "放行全部私聊", "仅白名单"):
        assert label in text, f"缺少策略文案：{label}"
    assert "后过期" in text, "配对码倒计时文案没了"
    # 调用点必须在卡片块里
    assert "feishu.dm_policy" in block, "没有切换私聊策略的调用"
    assert "feishu.pairing.create" in block, "没有生成配对码的调用"


def test_card_self_check_uses_probe_and_never_shows_token() -> None:
    block = _card_block()
    assert "feishu.probe" in block, "自检没走 feishu.probe"
    assert "机器人名" in block, "自检没显示机器人名"
    assert "token 值不回传" in block, "自检该说明 token 值不显示"
    assert "unknown" in block, "探测失败该回退显示 unknown"


def test_card_fixed_element_ids_exist_in_html() -> None:
    """卡片唯一用到的固定节点 id 是 settings-note —— 必须在 HTML 里存在。

    （卡片其余节点都是 `document.createElement` 动态生成；通用漂移守卫
    `test_practice_mode.py::test_all_element_ids_used_by_js_exist_in_html`
    已覆盖全项目 `el("x")` 引用，故此处只钉卡片自己用到的那一个。）
    """
    block = _card_block()
    used = set(re.findall(r'el\("([a-z0-9-]+)"\)', block))
    assert used, "卡片没通过 el() 取任何固定节点"
    html = INDEX_HTML_PATH.read_text(encoding="utf-8")
    for element_id in used:
        assert f'id="{element_id}"' in html, f"卡片引用的 id 在 HTML 里不存在：{element_id}"


def test_card_has_styles() -> None:
    css = STYLE_CSS_PATH.read_text(encoding="utf-8")
    for selector in (".feishu-status", ".feishu-code-value", ".feishu-probe", ".feishu-value"):
        assert selector in css, f"缺少样式 {selector}"


def test_app_js_still_uses_the_markdown_renderer() -> None:
    """顺带确认拆文件后 APP_JS 仍是合并文本（本测试的块提取依赖它）。"""
    assert "ScoutMarkdown" in APP_JS

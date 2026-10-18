"""设置面板两个可编辑项：`saveSettings` 的"留空/0 → null"与 `renderBudgetUsage` 接线。

两层：

1. **桩桥实测**（`settings_budget_probe.js`，node + `vm` 真跑 `saveSettings` / `renderSettingsField` /
   `renderBudgetUsage`）—— 断言的是**桩桥收到的 `settings.update` 入参**，不是界面文字。
   界面写"已保存"完全可以在一次请求都没发出去时成立，那种假绿在这里被排掉。
2. **接线断言**（读前端文本）—— 面板确实挂了这两个控件、用量行确实接在 `openSettings` 上。

为什么"留空"这条值得单独钉住：老实现是 `if (!text) return;`（跳过）。用户清空输入框的意思是
"把上限去掉"，跳过等于"保留原来的上限"，面板一关一开还是旧值 —— 而界面会显示"已保存并生效"。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ._ui import APP_JS, UI_DIR

APP_JS_PATH = UI_DIR / "app.js"
PROFILE_JS = UI_DIR / "profile.js"
PROBE_JS = Path(__file__).resolve().parent / "settings_budget_probe.js"


# --------------------------------------------------------------- 桩桥实测


@pytest.fixture(scope="module")
def probe() -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("没装 node，跳过设置面板的桩桥探针")
    assert PROBE_JS.is_file(), f"缺少探针脚本：{PROBE_JS}"
    assert APP_JS_PATH.is_file() and PROFILE_JS.is_file()
    completed = subprocess.run(
        [node, str(PROBE_JS), str(APP_JS_PATH), str(PROFILE_JS)],
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


def test_every_probe_check_passes(probe: dict[str, object]) -> None:
    checks = probe["checks"]
    assert isinstance(checks, list) and checks, "探针没跑出任何断言"
    failures = [f"{c['name']} —— {c['got']}" for c in checks if not c.get("ok")]
    assert not failures, "设置面板桩桥断言失败：\n" + "\n".join(failures)


def test_blank_budget_really_sends_null(probe: dict[str, object]) -> None:
    """留空保存 → 入参里**真的**有 `runtime.daily_token_budget: null`（不是"没发这个键"）。"""
    updates = probe.get("updates") or []
    assert updates, "一次 settings.update 都没发出去"
    first = updates[0]
    assert isinstance(first, dict)
    runtime = first.get("updates", {}).get("runtime", {})  # type: ignore[union-attr]
    assert "daily_token_budget" in runtime, f"入参里没有这个键：{first}"
    assert runtime["daily_token_budget"] is None, f"留空没能表达'不限'：{runtime}"


def test_budget_number_is_sent_as_int(probe: dict[str, object]) -> None:
    """填 500000 → 入参是数字（不是字符串，否则后端要按字符串分支去猜）。"""
    updates = probe.get("updates") or []
    got = [u["updates"]["runtime"]["daily_token_budget"] for u in updates if isinstance(u, dict)]  # type: ignore[index]
    assert 500000 in got, f"没有一次把 500000 作为上限发出去：{got}"
    assert all(not isinstance(v, str) for v in got if v is not None), f"上限被当成字符串发了：{got}"


def test_probe_covers_the_risky_areas(probe: dict[str, object]) -> None:
    """用例集别缩水：这几条面必须都在（少一条等于少一道防线）。"""
    names = " ".join(str(c["name"]) for c in probe["checks"])  # type: ignore[union-attr]
    for needle in ("留空", "填 0", "500000", "提醒", "已用", "超限", "不限量"):
        assert needle in names, f"探针少了「{needle}」这条断言：{names}"


# --------------------------------------------------------------- 接线断言


def test_budget_field_is_marked_nullable_and_has_usage_row() -> None:
    text = APP_JS
    assert "runtime.daily_token_budget" in text
    assert "nullable: true" in text, "上限字段没标 nullable，留空会被前端跳过"
    assert "usage: true" in text, "上限字段没标 usage，用量行不会画"


def test_open_settings_calls_render_budget_usage() -> None:
    """用量行必须在 `openSettings` 里被填上 —— 定义了却没人调 = 永远停在"读取中…"。"""
    block = _between(
        PROFILE_JS.read_text(encoding="utf-8"),
        "async function openSettings",
        "function closeSettings",
    )
    assert "await renderBudgetUsage()" in block, (
        "openSettings 没调 renderBudgetUsage，用量行一直空着"
    )


def test_save_settings_handles_nullable_before_the_blank_skip() -> None:
    """`field.nullable` 的分支必须**在** `if (!text) return;` 之前 —— 排在后面就永远进不去。"""
    text = PROFILE_JS.read_text(encoding="utf-8")
    block = _between(text, "async function saveSettings", 'await scout.call("settings.update"')
    nullable_at = block.index("field.nullable")
    skip_at = block.index("if (!text) return;")
    assert nullable_at < skip_at, "可空分支排在'留空跳过'后面，留空仍然会被跳过"


def test_notify_switch_is_in_the_channel_category() -> None:
    """渠道栏要能看到提醒开关（不放进去就等于没做这个开关）。"""
    # 结束锚点必须取 `SETTINGS_CATEGORIES` **之后**的声明：`SETTINGS_SPEC` 在它前面（1552 < 1665），
    # 拿它当结束锚点会切出空串 —— 曾经因此 ValueError: substring not found。
    spec = _between(APP_JS, "var SETTINGS_CATEGORIES = [", "var PROFILE_CATEGORIES = [")
    assert re.search(r'name:\s*"channel"[\s\S]*?sections:\s*\[[^\]]*跑完提醒', spec), (
        "「渠道」分类里没有「跑完提醒」这一组"
    )


def _between(text: str, start: str, end: str) -> str:
    at = text.index(start)
    return text[at : text.index(end, at)]

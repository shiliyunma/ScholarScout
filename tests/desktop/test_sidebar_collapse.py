"""左侧栏（会话）收起/展开的界面契约：键名、纯函数语义、接线与无障碍。

「键名 / 纯函数」这块最容易**静默出错**：改了键名不报错、把判定从 ``"1"`` 改成真值
界面照样能跑，只是刷新后状态丢了（用户会以为"这功能没做"）。所以这里既钉字面量，
也用 node 真跑一遍真值表（没装 node 就跳过，不让缺 node 的机器变红）。
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from ._ui import APP_JS, INDEX_HTML, UI_DIR

API_JS = UI_DIR / "api.js"
COLLAPSE_KEY = "scout:sidebarCollapsed"


def test_collapse_key_name_is_pinned() -> None:
    """存档键名钉死：改一个字，刷新后状态就再也读不回来。"""
    assert f'var SIDEBAR_COLLAPSE_KEY = "{COLLAPSE_KEY}";' in APP_JS


def test_pure_function_semantics_are_pinned() -> None:
    assert "function sidebarCollapsedFromStored(raw)" in APP_JS
    assert 'return raw === "1";' in APP_JS, '只有 "1" 算收起（没存过 / 坏值都当展开）'
    assert "function sidebarStoredValue(collapsed)" in APP_JS
    assert 'return collapsed ? "1" : "0";' in APP_JS


def test_toggle_is_wired_and_state_is_restored_on_load() -> None:
    assert 'el("btn-side-toggle").addEventListener("click", toggleSidebarCollapsed)' in APP_JS
    assert "applySidebarCollapsed(readSidebarCollapsed(), false)" in APP_JS, (
        "加载时必须按存档恢复收起态（persist=false，别在启动时回写）"
    )


def test_toggle_button_markup_carries_state_attributes() -> None:
    assert 'id="btn-side-toggle"' in INDEX_HTML
    assert 'aria-expanded="true"' in INDEX_HTML, "初始展开态要有 aria-expanded"
    assert 'aria-controls="session-list"' in INDEX_HTML


def test_pure_functions_truth_table_via_node() -> None:
    """用 node 真跑这两个纯函数，钉住 "1"/"0" 的往返与"坏值当展开"。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("没装 node，跳过收起态纯函数真值表")
    assert API_JS.is_file(), f"缺少 api.js：{API_JS}"
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(API_JS))}, 'utf8');
function slice(name) {{
  const i = src.indexOf('function ' + name);
  if (i < 0) throw new Error('找不到函数 ' + name);
  const j = src.indexOf('\\n}}', i);
  return src.slice(i, j + 2);
}}
eval(slice('sidebarCollapsedFromStored') + '\\n' + slice('sidebarStoredValue'));
const out = {{
  fromStored: {{
    one: sidebarCollapsedFromStored('1'),
    zero: sidebarCollapsedFromStored('0'),
    nothing: sidebarCollapsedFromStored(null),
    garbage: sidebarCollapsedFromStored('true'),
  }},
  toStored: {{ yes: sidebarStoredValue(true), no: sidebarStoredValue(false) }},
}};
console.log(JSON.stringify(out));
"""
    completed = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert completed.returncode == 0, f"node 跑挂了：{completed.stderr}"
    out = json.loads((completed.stdout or "").strip())
    assert out["fromStored"] == {"one": True, "zero": False, "nothing": False, "garbage": False}
    assert out["toStored"] == {"yes": "1", "no": "0"}

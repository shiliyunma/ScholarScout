"""py.exec 审批卡（`chat.needs_approval` / `sandbox.approve`）的行为契约测试。

四层，各钉一件事：

1. **node 真跑产品 js**（`approval_probe.js`）：把 `app.js` 里的 `approvalRiskLabel` /
   `approvalCardKey` / `approvalCards` / `approvalWorkdirParam` / `renderApprovalCard` /
   `answerApproval` 按名字切出来，用 `new Function` 把 `scout` / `streamBox` 等作为**形参**
   注入求值 —— 测的是产品文件里的那段源码，不是复刻版。断言"风险标签说人话、代码框里是
   完整代码、四按钮齐且顺序固定、点下去真的调了 `sandbox.approve` 且入参正确、连点只发一条、
   拒绝有那句『不要换写法重试』、非当前会话的卡不串流、同一轮同一段代码不重插"。
2. **跨层一句话对得上**：前端 `APPROVAL_LABELS` 的键必须与后端
   `sandbox.approval.approval_options()` 的 `value` **同一个集合** —— 少一个就等于某个
   按钮点了发一个后端不认的值（`-32602`，用户点了没反应）。
3. **`workdir` 的基准换算**（跨层，最容易静默错的一处）：载荷里是 `paths.rel_to_home`
   的形态（相对**项目根**，`data/exports`），`py.exec` 的参数是相对 **`data/`**。
   原样回传 → `data/data/exports`：用户批准的那个目录与实际执行的不是一个。
4. **样式与接线形状**：代码框必须**固定高度**（`--ui-approval-code-height`）且
   `overflow:auto`；`chat.needs_approval` 的路由必须在"必须有进行中气泡"那道闸**之前**
   （否则工具回包之后推来的卡会被静默丢掉）。

"真渲染 + 代码框真的滚（scrollHeight > clientHeight）+ 相邻栏位几何 + 截图"由
`run/ui_approval_probe.py`（无头 Edge + 桩桥）负责，那是另一层证据。

没装 node 就跳过（本机有；别人机器上不该因为缺 node 而红）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ._ui import APP_JS, STYLE_CSS, UI_DIR
from .test_composer_ui import ALLOWED_NEW_TOKENS

#: `_ui.py` 只导出合并文本 `APP_JS` 与单文件 `MARKDOWN_JS`/`STYLE_CSS`/`INDEX_HTML`，
#: 没有 `API_JS`。这里要的是**单个文件** `api.js` 的文本（下面用例要在它的路由序列里比
#: 两处锚点先后，合并文本里 `app.js` 排在前面会把顺序搅乱），所以按它自己导出的
#: `UI_DIR` 读那一个文件 —— 而不是往 `_ui.py` 里加一个只为这个测试存在的常量。
API_JS = (UI_DIR / "api.js").read_text(encoding="utf-8")
APP_JS_PATH = UI_DIR / "app.js"

ROOT = Path(__file__).resolve().parents[2]
PROBE_JS = Path(__file__).resolve().parent / "approval_probe.js"

#: 代码框高度的令牌名（本轮唯一新增的具名令牌）
CODE_TOKEN = "--ui-approval-code-height"

#: 拒绝时必须出现的那句话（`tools/py_exec.py` 被拦时的同一句：拒绝不是"换个写法再来"）
DENY_SENTENCE = "不要换写法重试，也不要换条路达成同一目的"


@pytest.fixture(scope="module")
def probe_results() -> list[dict[str, object]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("没装 node，跳过审批卡的 node 用例")
    assert PROBE_JS.is_file(), f"缺少探针源码：{PROBE_JS}"
    # 探针的入参是**文件路径**（它自己 fs.readFileSync），不是 app.js 的正文：
    # 把 87KB 源码当 argv 传，在 Windows 上直接 `[WinError 206] 文件名或扩展名太长`
    # （cmd.exe 包装后命令行上限 8191/32767，实测这里就炸了）。
    completed = subprocess.run(
        [node, str(PROBE_JS), str(APP_JS_PATH)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    assert completed.returncode == 0, f"node 跑挂了：{completed.stderr}"
    payload = (completed.stdout or "").strip()
    assert payload, (
        "node 没有任何输出（探针在加载阶段就抛了？stderr：" + completed.stderr[:800] + "）"
    )
    return json.loads(payload)


def test_every_approval_case_passes(probe_results: list[dict[str, object]]) -> None:
    failures = [f"{case['name']} —— {case['got']}" for case in probe_results if not case.get("ok")]
    assert not failures, "审批卡用例失败：\n" + "\n".join(failures)


def test_probe_covers_the_risky_areas(probe_results: list[dict[str, object]]) -> None:
    """用例集本身别缩水：这几面各少一条就等于少一道防线。"""
    names = " ".join(str(case["name"]) for case in probe_results)
    for keyword in ("风险标签", "完整", "四个选项", "连点两次", "非当前会话", "拒绝"):
        assert keyword in names, f"用例集缺少对「{keyword}」的覆盖"
    assert len(probe_results) >= 20, f"用例太少（{len(probe_results)} 条），是不是被删了"


# ------------------------------------------------------------------ 跨层：四选项


def _labels() -> dict[str, str]:
    """从产品源码里抠出 `var APPROVAL_LABELS = {...};` 的键值对。"""
    match = re.search(r"var APPROVAL_LABELS = \{(.*?)\};", APP_JS, re.S)
    assert match, "app.js 里找不到 var APPROVAL_LABELS = {...};"
    pairs = dict(re.findall(r"(\w+):\s*\"([^\"]+)\"", match.group(1)))
    assert pairs, "APPROVAL_LABELS 解析出 0 条 —— 写法变了，先修这个解析再谈断言"
    return pairs


def _order() -> list[str]:
    match = re.search(r"var APPROVAL_ORDER = \[(.*?)\];", APP_JS, re.S)
    assert match, "app.js 里找不到 var APPROVAL_ORDER = [...];"
    order = re.findall(r"\"(\w+)\"", match.group(1))
    assert order, "APPROVAL_ORDER 解析出 0 条"
    return order


def test_frontend_labels_cover_exactly_the_backend_values() -> None:
    """前端文案表与后端 `approval_options()` 必须是**同一个集合**。

    少一个 → 那个按钮点了发一个后端不认的 value（`-32602`，用户点了没反应）；
    多一个 → 界面上会出现一个永远不能生效的选项。
    """
    from scholarscout.sandbox.approval import approval_options

    backend = {item["value"] for item in approval_options()}
    frontend = set(_labels())
    assert frontend == backend, f"前端文案 {sorted(frontend)} ≠ 后端 value {sorted(backend)}"


def test_render_order_is_the_same_four_values_with_deny_last() -> None:
    """四个按钮的顺序固定：最小授权 → 逐级放宽 → **拒绝最后**（误点代价最大的一条不摆第一格）。"""
    from scholarscout.sandbox.approval import approval_options

    order = _order()
    assert order == ["once", "session", "always", "deny"], f"渲染顺序变了：{order}"
    assert set(order) == {item["value"] for item in approval_options()}


def test_deny_sentence_is_the_exact_one() -> None:
    """拒绝文案是**指定那句**：换成"已取消"就丢掉了"别换条路"的约束。"""
    match = re.search(r"var APPROVAL_DENY_NOTE = \"([^\"]+)\";", APP_JS)
    assert match, "app.js 里找不到 var APPROVAL_DENY_NOTE"
    assert DENY_SENTENCE in match.group(1), f"拒绝文案不含指定句子：{match.group(1)}"


# ------------------------------------------------- 样式：固定高 + 内部滚动（不撑高对话）


def _rule(selector: str) -> str:
    """取 `.approval-code` 这类简单选择器的声明块（先剥注释，否则 banner 里的花括号会切歪）。"""
    stripped = re.sub(r"/\*.*?\*/", "", STYLE_CSS, flags=re.S)
    bodies = [
        body
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", stripped)
        if " ".join(sel.split()) == selector
    ]
    return " ".join(" ".join(body.split()) for body in bodies)


def test_code_box_has_a_fixed_height_and_scrolls_inside() -> None:
    """代码框**必须有固定高度**并 `overflow:auto`：撑开就等于把对话顶出屏幕。

    断言 height（不只是 max-height）：只有 max-height 时短代码会把卡片压成一条，
    长代码又把消息流撑高 —— 两种都不算"固定高度"。
    """
    body = _rule(".approval-code")
    assert body, "style.css 里找不到 .approval-code 规则"
    assert f"height: var({CODE_TOKEN})" in body, f"代码框没有固定高度：{body!r}"
    assert f"max-height: var({CODE_TOKEN})" in body, f"代码框没有高度上限：{body!r}"
    assert "overflow: auto" in body, f"代码框不在内部滚动：{body!r}"
    assert "font-family: var(--font-mono)" in body, f"代码框不是等宽字体：{body!r}"
    assert "white-space: pre" in body, f"代码框按自动折行排版（缩进会错位）：{body!r}"


def test_code_height_token_is_declared_once_and_whitelisted() -> None:
    """高度只能有**一个来源**：`--ui-approval-code-height` 定义一次，且在本轮白名单里。

    两个来源（例如再写一遍 px）迟早漂移：一处改了另一处还老值，代码框就"说好的固定高"失效。
    """
    declarations = re.findall(rf"({CODE_TOKEN})\s*:", STYLE_CSS)
    assert len(declarations) == 1, f"{CODE_TOKEN} 被定义了 {len(declarations)} 次（只许一处）"
    assert CODE_TOKEN in ALLOWED_NEW_TOKENS, (
        f"{CODE_TOKEN} 没进 test_composer_ui.ALLOWED_NEW_TOKENS"
    )


def test_note_row_is_hidden_by_default() -> None:
    """结果/文案行默认隐藏，且显式补 `[hidden]` 的 display:none（类选择器会压掉 UA 的规则）。"""
    assert ".approval-note[hidden]" in STYLE_CSS, "缺 .approval-note[hidden] 的 display:none"
    body = _rule(".approval-note[hidden]")
    assert "display: none" in body, f"[hidden] 没被强制隐藏：{body!r}"


# ------------------------------------------------------ 接线：路由位置与状态表


def test_needs_approval_is_routed_before_the_pending_bubble_gate() -> None:
    """`chat.needs_approval` 必须在"没有进行中气泡就不动界面"那道闸**之前**处理。

    审批卡本来就是工具回包（`ok=false + kind="needs_approval"`）之后推来的，
    这一轮随时可能已经收尾；放到闸后面，卡就会被 `if (!slot || !slot.trace) return;`
    静默丢掉 —— 界面上"点了没反应"的又一种来源。
    """
    handler = API_JS.index('if (event.method === "chat.needs_approval")')
    gate = API_JS.index("if (!slot || !slot.trace) return;")
    assert handler < gate, "chat.needs_approval 的路由跑到进行中气泡闸后面了（卡会被丢掉）"
    assert "renderApprovalCard(key, p);" in API_JS[handler : handler + 200]


def test_card_table_lives_in_state_next_to_the_choice_table() -> None:
    """每会话一张审批卡表（去重键 `turn_id|code`）挂在 `state` 上，与 `state.choices` 同级。"""
    assert re.search(r"\n  approvals: \{\},", APP_JS), "state 里没有 approvals 表"
    block = APP_JS[
        APP_JS.index("function approvalCards(") : APP_JS.index("function approvalTitleText(")
    ]
    assert "state.approvals[key]" in block, "approvalCards 没读 state.approvals（去重会失效）"


def test_card_is_appended_to_its_own_session_stream() -> None:
    """卡片挂 `streamBox(session_id)` —— 不是"此刻显示的那条流"：不串会话是结构保证。"""
    block = APP_JS[
        APP_JS.index("function renderApprovalCard(") : APP_JS.index(
            "async function answerApproval("
        )
    ]
    assert "streamBox(key).appendChild(wrap);" in block, "卡片没有挂进它自己会话的流"
    assert "if (!key) return null;" in block, "没有 session_id 时也该拒收（不落任何人的流）"


def test_click_calls_the_approve_rpc_with_the_original_code() -> None:
    """点按钮 = 一次 `sandbox.approve`，**原样带回 code**（改了 code 就是批准一段没看过的代码）。"""
    block = APP_JS[
        APP_JS.index("async function answerApproval(") : APP_JS.index("async function send(text)")
    ]
    assert 'scout.call("sandbox.approve"' in block, "点击没有调 sandbox.approve"
    assert "code: code" in block, "code 没带回去"
    assert "session_id: sessionId" in block, "session_id 没带回去（规则记忆会记到别的会话）"
    assert 'card.setAttribute("data-approved", approval);' in block, "没有「先禁卡再发」的顺序"

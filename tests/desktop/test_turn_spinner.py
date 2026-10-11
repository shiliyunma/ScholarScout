"""工具调用中的「加载圈」：界面契约 + 状态机真值表。

用户反馈是"工具调用的间隙里模型在『想』，界面上什么都不动，不知道它活着"。
这里钉住三件最容易**静默出错**的事：

1. 加载圈是**浮层**（绝对定位），不许把消息流顶移位；
2. 「事件序列 → 加载圈状态」的映射，尤其**结束后必须消失**（防永久转圈）；
3. 样式只用本设计真实存在的 ``--ui-*`` 令牌 —— 写了别的名字会让整条声明静默失效。

状态机是纯函数、住在 ``api.js``；这里用 node 把函数**切出来**真跑（与
``test_sidebar_collapse.py`` 同一手法），没装 node 就跳过（不让缺 node 的机器变红）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from ._ui import APP_JS, INDEX_HTML, STYLE_CSS, UI_DIR

API_JS = UI_DIR / "api.js"

#: 这套设计令牌体系是 --ui-* / --radius-* / --shadow-* 家族。下面这些名字在
#: style.css 里**没有任何定义**，写进声明里等于把该声明废掉（计算值阶段静默失败）。
LEGACY_TOKENS = ("--border", "--foreground", "--muted-foreground", "--accent", "--background")


def _rules(css: str) -> list[tuple[str, str]]:
    """粗切「选择器 → 声明块」；先剥注释，否则注释里的花括号会把规则切歪。"""
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", stripped):
        sel = " ".join(selector.split())
        if sel:
            out.append((sel, " ".join(body.split())))
    return out


# ------------------------------------------------------------------ HTML / 接线


def test_spinner_markup_is_declared_and_hidden() -> None:
    """元素必须静态声明（JS 用 el() 取），且默认 hidden（没发起一轮时不该转）。"""
    assert 'id="turn-spinner"' in INDEX_HTML, "index.html 里没有加载圈节点"
    assert 'id="turn-spinner-text"' in INDEX_HTML, "没有加载圈文案节点"
    assert 'role="status"' in INDEX_HTML and 'aria-live="polite"' in INDEX_HTML
    assert re.search(r'id="turn-spinner"[^>]*\bhidden\b', INDEX_HTML), "加载圈默认必须是 hidden"


def test_spinner_state_machine_functions_exist() -> None:
    assert "function turnSpinnerNext(" in APP_JS, "缺纯函数 turnSpinnerNext"
    assert "function turnSpinnerText(" in APP_JS, "缺纯函数 turnSpinnerText"
    assert "function turnSpinnerUpdate(" in APP_JS
    assert "function turnSpinnerReset(" in APP_JS


def test_send_lights_on_start_and_forces_reset() -> None:
    """发起一轮点亮；finally 里**无条件**复位 —— 这是"结束后必消失"的最后一道保险。"""
    assert 'turnSpinnerUpdate("turn.start")' in APP_JS, "send() 没有在发起一轮时点亮加载圈"
    assert re.search(r"finally\s*\{[^}]*turnSpinnerReset\(\)", APP_JS), (
        "finally 里必须无条件 turnSpinnerReset()，否则后端没回 chat.done 就会永久转圈"
    )


def test_scout_event_routes_events_into_spinner() -> None:
    assert "turnSpinnerUpdate(event.method, p);" in APP_JS, "scoutEvent 没把事件喂给加载圈"
    assert APP_JS.index("turnSpinnerUpdate(event.method, p);") > APP_JS.index(
        "const trace = state.pending;"
    ), "必须放在 pending 门之后：一轮没在跑就不该动加载圈"


def test_element_ids_used_by_spinner_exist_in_html() -> None:
    for name in ("turn-spinner", "turn-spinner-text"):
        assert f'id="{name}"' in INDEX_HTML
        assert f'el("{name}")' in APP_JS


# ------------------------------------------------------------------ 样式守卫


def test_spinner_is_absolute_overlay() -> None:
    """必须是绝对定位浮层 —— 否则它会进消息流、把消息流顶移位。"""
    spinner = [body for sel, body in _rules(STYLE_CSS) if sel == ".turn-spinner"]
    assert spinner, "样式里找不到 .turn-spinner 规则"
    body = " ".join(spinner)
    assert "position: absolute" in body, "加载圈必须是 position:absolute 的浮层"
    assert "bottom: calc(100% + 8px)" in body, "应当浮在输入框上方（bottom: calc(100% + 8px)）"
    # 定位上下文：.composer 得有 position: relative，否则浮层会以别的祖先为准、跑到别处
    composers = [body for sel, body in _rules(STYLE_CSS) if sel == ".composer"]
    assert any("position: relative" in b for b in composers), (
        ".composer 需要 position:relative 当定位上下文"
    )


def test_hidden_is_forced_display_none() -> None:
    """类选择器上的 display 会压掉 UA 给 [hidden] 的 display:none —— 必须显式补一条。"""
    hidden = [body for sel, body in _rules(STYLE_CSS) if sel == ".turn-spinner[hidden]"]
    assert hidden, "缺 .turn-spinner[hidden] 规则"
    assert any("display: none" in body for body in hidden), (
        ".turn-spinner[hidden] 必须 display:none"
    )


def test_spinner_styles_avoid_undefined_tokens() -> None:
    offenders = []
    for sel, body in _rules(STYLE_CSS):
        if "turn-spinner" not in sel:
            continue
        for token in LEGACY_TOKENS:
            if f"var({token})" in body:
                offenders.append((sel, token))
    assert not offenders, (
        f"加载圈样式用了未定义的旧令牌 {offenders} —— "
        "改用 --ui-stroke-secondary / --ui-text-primary / --ui-bg-elevated 等真实令牌"
    )


def test_spinner_animation_has_no_external_resource() -> None:
    """离线可用：加载圈的动效只能是本地 CSS 关键帧，不能引外部资源。"""
    block = re.sub(r"/\*.*?\*/", "", STYLE_CSS, flags=re.S)
    assert "@keyframes turn-spinner-spin" in block, "缺本地关键帧 turn-spinner-spin"
    assert "http://" not in block and "https://" not in block


# ---------------------------------------------------------- 状态机真值表（node）


@pytest.fixture(scope="module")
def spinner_probe() -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("没装 node，跳过加载圈状态机真值表")
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
eval(slice('turnSpinnerNext') + '\\n' + slice('turnSpinnerText'));

/* 按事件序列跑一遍：返回每一步之后的 {{visible, text}}。 */
function run(events) {{
  let state = null;
  const seen = [];
  for (const ev of events) {{
    const next = turnSpinnerNext(state, ev.method, ev.params || {{}});
    if (next !== null) state = next;
    seen.push({{ visible: !!(state && state.visible === true), text: turnSpinnerText(state) }});
  }}
  return seen;
}}

const out = {{
  full: run([
    {{ method: 'turn.start' }},
    {{ method: 'chat.tool_call', params: {{ tool: 'research.search' }} }},
    {{ method: 'chat.tool_result', params: {{ ok: true, latency_ms: 12 }} }},
    {{ method: 'chat.delta', params: {{ text: '正文' }} }},
    {{ method: 'chat.done', params: {{ stop_reason: 'completed' }} }},
  ]),
  errorEnds: run([
    {{ method: 'turn.start' }},
    {{ method: 'chat.tool_call', params: {{ tool: 'source.fetch' }} }},
    {{ method: 'chat.error', params: {{ message: 'boom' }} }},
  ]),
  /* 与加载圈无关的事件必须返回 null（不动状态），别把别人的事件吃掉。 */
  unrelatedIsNull: [
    turnSpinnerNext(null, 'session.updated', {{}}),
    turnSpinnerNext(null, 'practice.delta', {{}}),
    turnSpinnerNext(null, 'chat.subtask_done', {{}}),
  ],
  /* 静默期不变：工具执行中持续没有事件时，文案仍是"正在调用 …工具"。 */
  silenceText: turnSpinnerText(turnSpinnerNext(null, 'chat.tool_call', {{ tool: 'topic.build_map' }})),
  noToolName: turnSpinnerText(turnSpinnerNext(null, 'chat.tool_call', {{}})),
  idleHidden: turnSpinnerText(null),
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
    return json.loads((completed.stdout or "").strip())


def test_full_sequence_mapping(spinner_probe: dict[str, object]) -> None:
    steps = spinner_probe["full"]
    assert [step["visible"] for step in steps] == [True, True, True, True, False], (
        "turn.start/tool_call/tool_result/delta 都该显示，chat.done 后必须消失"
    )
    assert steps[0]["text"] == "正在思考…"
    assert steps[1]["text"] == "正在调用 research.search 工具", "调用中必须带工具名"
    assert steps[2]["text"] == "等待下一个动作…", "结果后换成等待文案"
    assert steps[3]["text"] == "正在生成回答…"
    assert steps[4]["text"] == "", "结束后文案必须清空"
    assert steps[4]["visible"] is False, "结束后必须消失（防永久转圈）"


def test_error_also_hides_the_spinner(spinner_probe: dict[str, object]) -> None:
    steps = spinner_probe["errorEnds"]
    assert [step["visible"] for step in steps] == [True, True, False]
    assert steps[-1]["visible"] is False


def test_unrelated_events_do_not_touch_state(spinner_probe: dict[str, object]) -> None:
    assert spinner_probe["unrelatedIsNull"] == [None, None, None], (
        "与加载圈无关的事件必须返回 null（不动状态）"
    )


def test_silence_keeps_the_tool_text(spinner_probe: dict[str, object]) -> None:
    assert spinner_probe["silenceText"] == "正在调用 topic.build_map 工具"
    assert spinner_probe["noToolName"] == "正在调用工具…"
    assert spinner_probe["idleHidden"] == ""

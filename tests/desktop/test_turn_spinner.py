"""会话流内的「加载圈 + 工具条目 + 每轮正文」：界面契约 + 状态机真值表。

上一轮的方案是**浮层**：加载圈绝对定位在 .composer 上方飘着，工具调用只往 .trace 里
写两行文字，模型每一轮的正文都**覆盖**同一个正文区（所以只有最后一轮留得下）。
用户这轮把方案改了，于是这个文件的断言**整套换掉**（旧断言必红，不要"缝"）：

1. 加载圈在**会话消息流内部**（「进行中」气泡里的 .msg-progress），不是输入框上方的浮层；
2. `chat.done` / `chat.error` 之后**必消失**（两重保证：正常路径移除节点 + finally 兜底清扫）；
3. 工具条目数 = 工具调用数（一次调用一条 .tool-row，结果**补齐**那一行而不是再建一行）；
4. 每轮正文各自成段（.reply-round）**不互盖**；
5. 样式只用本设计真实存在的 ``--ui-*`` 令牌 —— 写了别的名字会让整条声明静默失效。

分工（刻意的）：**纯函数真值表**在这里用 node 切出来真跑（与 ``test_sidebar_collapse.py``
同一手法，没装 node 就跳过，不让缺 node 的机器变红）；**真实 DOM 的条数**（工具行数、
段数、几何不动）由 ``run/ui_chat_probe.py`` 在无头 Edge 里量 —— 那里才是真渲染，
这里钉的是"代码形状不会悄悄退回旧方案"。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ._ui import APP_JS, INDEX_HTML, STYLE_CSS, UI_DIR

API_JS_PATH = UI_DIR / "api.js"
API_JS = API_JS_PATH.read_text(encoding="utf-8")

#: 这套设计令牌体系是 --ui-* / --radius-* / --shadow-* 家族。下面这些名字在
#: style.css 里**没有任何定义**，写进声明里等于把该声明废掉（计算值阶段静默失败）。
#: ``--ui-border`` 是老样式里靠 ``var(--ui-border, rgba(...))`` 兜底在用的名字，
#: 它同样没有定义 —— 新写的样式一律不许再用。
LEGACY_TOKENS = (
    "--border",
    "--foreground",
    "--muted-foreground",
    "--accent",
    "--background",
    "--ui-border",
)

#: 本轮新增样式块的 banner（style.css 里唯一）。样式守卫只审**这块之后**的规则：
#: 老样式里有历史遗留的 --border / --foreground（.chat-head 的描边与标题色），
#: 整表扫描会把历史债算到本轮头上 —— 那不是本轮该改的东西。
NEW_CSS_BANNER = "②③ 会话流内的加载圈"

#: 改前实测的相邻栏位几何（无头 Edge，窗口 1680x1000）。加载圈从浮层搬进消息流后，
#: 这几条**必须一个数都不变** —— 改布局只断言自己那一块是最容易漏掉真 bug 的。
BASELINE_GEOM = {
    "left": 216,
    "splitLeft": 6,
    "chat": 1042,
    "splitRight": 6,
    "right": 380,
}


def _rules(css: str) -> list[tuple[str, str]]:
    """粗切「选择器 → 声明块」；先剥注释，否则注释里的花括号会把规则切歪。"""
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", stripped):
        sel = " ".join(selector.split())
        if sel:
            out.append((sel, " ".join(body.split())))
    return out


def _new_rules() -> list[tuple[str, str]]:
    """本轮新增那块样式的规则（按 banner 切分，见 NEW_CSS_BANNER 的说明）。

    切片起点要退到 banner 注释的 ``/*`` **开头**：从注释文字里切的话 ``/*`` 被切掉，
    注释就剥不干净，整段 banner 会被当成一个选择器（踩过）。
    """
    marker = STYLE_CSS.index(NEW_CSS_BANNER)
    return _rules(STYLE_CSS[STYLE_CSS.rindex("/*", 0, marker) :])


def _body_of(rules: list[tuple[str, str]], selector: str) -> str:
    """某个选择器的全部声明块拼起来（同一选择器可能出现在常规 + 媒体查询里）。"""
    return " ".join(body for sel, body in rules if sel == selector)


# ------------------------------------------------------------------ HTML / 接线


def test_loading_circle_is_no_longer_an_overlay_in_the_composer() -> None:
    """浮层方案要**拆干净**：节点、状态机入口、样式规则一个都不能留。

    留一个就够出鬼：例如样式还在、节点没了 → 看着"改了"，其实是半截。
    """
    assert 'id="turn-spinner"' not in INDEX_HTML, "index.html 里还留着旧的浮层加载圈节点"
    assert 'id="turn-spinner-text"' not in INDEX_HTML
    assert "turn-spinner" not in STYLE_CSS, "style.css 里还留着 .turn-spinner 的规则"
    assert "turn-spinner" not in API_JS, "api.js 里还引着 turn-spinner"


def test_composer_actions_only_holds_buttons() -> None:
    """「发送」那一行**只留发送**：同行的装饰文案（#hint）已删，用量已搬到标题右侧。"""
    assert 'id="hint"' not in INDEX_HTML, "index.html 里还留着发送旁的装饰文案 #hint"
    block = re.search(r'<div class="composer-actions">(.*?)</div>', INDEX_HTML, re.S)
    assert block, "找不到 .composer-actions"
    inner = block.group(1)
    assert "<span" not in inner, f"发送那一行还有装饰性 span：{inner.strip()[:120]}"
    assert 'id="btn-send"' in inner and 'id="btn-stop"' in inner


def test_session_usage_moved_next_to_the_title() -> None:
    """用量从发送那行搬到标题右侧，**id 不变**（refreshSessionUsage() 一字未改）。"""
    head = re.search(r'<div class="chat-head">(.*?)</div>', INDEX_HTML, re.S)
    assert head, "找不到 .chat-head"
    assert 'id="session-usage"' in head.group(1), "用量没有挂到标题右侧"
    assert 'id="session-usage"' not in re.search(
        r'<form class="composer".*?</form>', INDEX_HTML, re.S
    ).group(0), "用量还留在输入区里"


# ------------------------------------------------------------------ JS 接线（新方案）


def test_pending_bubble_owns_the_progress_row() -> None:
    """加载圈建在「进行中」气泡**内部**（node.body.appendChild(progress)），不是浮层；
    并且这个气泡挂在**它自己会话的那条流**里（makePending(sessionId)）。"""
    make_pending = APP_JS[APP_JS.index("function makePending(") : APP_JS.index("function paint()")]
    assert 'progress.className = "msg-progress";' in make_pending
    assert 'progressText.className = "msg-progress-text";' in make_pending
    assert "progress.hidden = true;" in make_pending, "初始必须是 hidden（还没发起一轮）"
    # 关键：挂在气泡 body 里 —— 这才是"消息流内"
    assert "node.body.appendChild(progress);" in make_pending
    assert 'el("messages").appendChild(progress)' not in make_pending, (
        "加载圈不该直接挂到消息流根上（那样会掉出「进行中」气泡）"
    )
    # 气泡必须落进"这个会话自己的流"（docs/20 第 5 步：串会话的正解）
    assert 'bubble("assistant", "ScholarScout · 进行中", sessionId)' in make_pending, (
        "makePending 没把 sessionId 传下去 → 气泡会挂到「此刻显示的那条流」上（老 bug）"
    )


def test_sweep_removes_every_leftover_progress_row() -> None:
    """兜底清扫：把**这个会话的流里**的 .msg-progress 摘掉（幂等）。
    只扫自己那条 —— 扫全局会把别的会话正在转的圈也摘掉。"""
    assert "function turnSpinnerSweep(" in API_JS, "缺兜底清扫 turnSpinnerSweep"
    sweep = API_JS[API_JS.index("function turnSpinnerSweep(") :]
    sweep = sweep[: sweep.index("\n}")]
    assert 'querySelectorAll(".msg-progress")' in sweep
    assert "node.remove()" in sweep, "清扫必须是**移除节点**，不是 hidden=true"
    assert "slot.trace.node.wrap" in sweep, "清扫必须限定在这个会话自己的流里"


def test_reset_hides_and_sweeps() -> None:
    """turnSpinnerReset(slot) = 正常路径收 + 兜底清扫，两道一起上。"""
    reset = API_JS[API_JS.index("function turnSpinnerReset(") :]
    reset = reset[: reset.index("\n}")]
    assert "slot.trace.hideProgress()" in reset
    assert "turnSpinnerSweep(slot, sessionId)" in reset


def test_send_lights_on_start_and_never_leaves_a_permanent_spinner() -> None:
    """发起一轮点亮；`mode=\"async\"` 下**不能**在 finally 里无条件复位（整轮还在跑），
    但"永久转圈"这条保险仍然要有：只有这一轮没在跑（发起就失败）时才复位。"""
    assert 'turnSpinnerUpdate(slot, "turn.start")' in APP_JS, "send() 没有在发起一轮时点亮加载圈"
    send_block = APP_JS[
        APP_JS.index("async function send(text)") : APP_JS.index("async function loadSessions")
    ]
    # 闸按会话判，不是全局布尔
    assert "if (sessionRunning(sessionId))" in send_block
    # async：整轮结论走 chat.done，收尾在 finishTurn
    assert 'mode: "async"' in send_block, "send() 没走 chat.send 的 async 模式"
    finally_block = send_block[send_block.rindex("finally {") :]
    finally_block = finally_block[: finally_block.index("\n  }")]
    assert "if (!slot.running) turnSpinnerReset(slot, sessionId);" in finally_block, (
        "finally 里必须按「这一轮还在不在跑」决定收不收加载圈："
        "无条件收会让 async 下的圈提前消失，不收又会在发起就失败时永久转圈"
    )
    # chat.done 那条路径必须真的收圈（否则 async 下永远转），而且**按会话**收
    finish = APP_JS[APP_JS.index("function finishTurn(") : APP_JS.index("async function send(")]
    assert "turnSpinnerReset(slot, sessionId);" in finish, (
        "finishTurn（chat.done）里没有按会话收掉加载圈"
    )
    assert finish.index("turnSpinnerReset(slot, sessionId);") < finish.index(
        "slot.trace = null;"
    ), "收加载圈必须在 slot.trace 置空**之前**：置空后再收会退回到「当前显示的流」，摘掉别人的圈"


def test_update_routes_visible_to_show_and_invisible_to_hide() -> None:
    """状态机 → DOM 的分流：visible 才 show，其余一律 hide（done/error 走的就是 hide）。"""
    update = API_JS[API_JS.index("function turnSpinnerUpdate(") :]
    update = update[: update.index("\n}")]
    assert "trace.showProgress(turnSpinnerText(next), next.kind, next.tool)" in update
    assert "else trace.hideProgress();" in update
    assert "if (!trace || next === null) return;" in update, (
        "一轮没在跑（没有 pending）就不该动界面"
    )


def test_scout_event_routes_events_into_the_new_handlers() -> None:
    """事件按 session_id 找槽，再喂给那条流的处理器（docs/20 第 5 步）。"""
    assert "turnSpinnerUpdate(slot, event.method, p);" in API_JS, "scoutEvent 没把事件喂给加载圈"
    assert "slot.trace.addToolCall(p)" in API_JS and "slot.trace.addToolResult(p)" in API_JS
    assert "slot.trace.pushDelta(p.text" in API_JS, "增量没有走分段渲染"
    # 必须先按 session_id 找到槽，再动界面（顺序反了就是"落到此刻显示的那个气泡上"）
    assert API_JS.index("turnSpinnerUpdate(slot, event.method, p);") > API_JS.index(
        "const key = eventSessionKey(event.method, p);"
    )
    # 收尾单独一条：chat.done 走 finishTurn（角标/计数在那里判）
    assert API_JS.index('if (event.method === "chat.done")') < API_JS.index(
        "turnSpinnerUpdate(slot, event.method, p);"
    ), "chat.done 必须在通用分派之前被单独接走"


def test_one_tool_row_per_call_and_result_never_adds_a_row() -> None:
    """**工具条目数 = 工具调用数**：一次调用建一行，结果只补齐那一行。

    这是最容易被"顺手重构"破坏的一条：结果里再新建一行，界面就会变成
    「调用一行 + 结果一行」= 两倍条目，看着像工具被调用了两次。
    """
    call = APP_JS[
        APP_JS.index("addToolCall: function (params)") : APP_JS.index(
            "addToolResult: function (params)"
        )
    ]
    assert call.count("createRow(") == 1, (
        f"一次调用只能建一行，现在是 {call.count('createRow(')} 次"
    )
    assert "openRows[String(p.call_id)] = entry;" in call, "没有按 call_id 记账，结果配不上对"
    assert "roundClosed = true" in call, "一次工具调用 = 这一轮的正文流到此收尾（分段判据）"

    result = APP_JS[APP_JS.index("addToolResult: function (params)") :]
    result = result[: result.index("pushDelta: function (text)")]
    assert "createRow(" not in result.split("entry = createRow(")[0], "结果处理的第一步不该是新建行"
    assert "let entry = key ? openRows[key] : null;" in result, "先按 call_id 收（配对失败才兜底）"
    assert "entry = lastRow;" in result, "call_id 对不上时的兜底：补到最近那一行"
    # 只允许在"后端只推了结果没推调用"这一种理论上不会发生的情况下补一行
    assert result.count("createRow(") == 1
    assert "entry.status.textContent = toolResultSummary(p);" in result
    assert "entry.latency.textContent = toolLatencyText(p, entry.startedAt, Date.now());" in result


def test_each_round_of_text_gets_its_own_paragraph() -> None:
    """**每轮正文各自成段不互盖**：一轮一段 .reply-round，绝不退回"覆盖同一个正文区"。"""
    assert 'currentRound.className = "reply-round";' in APP_JS
    assert "textBox.appendChild(currentRound);" in APP_JS
    push = APP_JS[APP_JS.index("pushDelta: function (text)") :]
    push = push[: push.index("setFinal: function (text)")]
    assert "const next = replyRoundNext(roundClosed, text);" in push
    assert "if (next.isNew || !currentRound) newRound();" in push
    # 旧的覆盖式接口必须**不存在**了（留着就等于随时能退回去）
    assert "setText:" not in APP_JS, "旧的覆盖式 setText 还在"
    assert "let buffered" not in APP_JS, "旧的单缓冲 buffered 还在"


def test_final_answer_lands_in_the_last_round() -> None:
    """收尾用后端给的**权威全文**落到最后一段 —— 界面与落库必须一致（标记被剥掉等情况）。

    async 下这句在 `finishTurn`（`chat.done`）里，不在 `send()` 的返回值里。
    """
    assert "trace.setFinal(p.text);" in APP_JS
    final = APP_JS[APP_JS.index("setFinal: function (text)") :]
    final = final[: final.index("showProgress: showProgress")]
    assert "roundClosed = true;" in final
    assert "finished = true;" in final and "flush();" in final


def test_stop_turn_falls_back_to_a_flow_notice() -> None:
    """轮次已收尾时点「停止」→ 落一条**本会话流**里的提示，绝不静默丢反馈（不再借 #hint）。"""
    assert "else notice(note, sessionId);" in APP_JS
    assert 'el("hint")' not in APP_JS, "还在往发送旁那个已删的 #hint 里写东西"
    assert 'wrap.className = "msg notice";' in API_JS


# ------------------------------------------------------------------ 样式守卫


def test_progress_row_is_not_an_overlay_and_is_low_key() -> None:
    """加载圈是气泡内的状态行：不许绝对定位、不许浮在输入框上方、不许比正文抢眼。"""
    rules = _new_rules()
    body = _body_of(rules, ".msg-progress")
    assert body, "样式里找不到 .msg-progress 规则"
    assert "position: absolute" not in body and "position:absolute" not in body
    assert "bottom: calc(100% + 8px)" not in body
    assert "display: inline-flex" in body, "[hidden] 的隐藏要靠显式 display:none 补回来"
    # 低调：等宽 + 小字号 + 三级文字色
    assert "font-family: var(--font-mono)" in body
    assert "font-size: 11.5px" in body
    assert "color: var(--ui-text-tertiary)" in body
    # 定位上下文（.composer 的 position:relative）随浮层方案一起退休
    composer = _body_of(rules, ".composer")
    assert "position: relative" not in composer, "浮层没了，.composer 不该还留定位上下文"


def test_progress_hidden_is_forced_display_none() -> None:
    """类选择器上的 display 会压掉 UA 给 [hidden] 的 display:none —— 必须显式补一条。"""
    rules = _new_rules()
    hidden = _body_of(rules, ".msg-progress[hidden]")
    assert hidden, "缺 .msg-progress[hidden] 规则"
    assert "display: none" in hidden


def test_tool_rows_are_compact_and_monospaced() -> None:
    """工具行：11px 等宽、三级色、行间 2px；参数摘要省略号收尾；语义色只上状态点。"""
    rules = _new_rules()
    row = _body_of(rules, ".tool-row")
    assert row, "样式里找不到 .tool-row 规则"
    assert "display: flex" in row
    assert "font-family: var(--font-mono)" in row
    assert "font-size: 11px" in row and "line-height: 1.5" in row
    assert _body_of(rules, ".tool-flow").find("gap: 2px") >= 0
    for name in (".tool-row-name", ".tool-row-args", ".tool-row-status", ".tool-row-latency"):
        assert _body_of(rules, name), f"缺 {name} 规则"
    args = _body_of(rules, ".tool-row-args")
    assert "text-overflow: ellipsis" in args and "min-width: 0" in args, (
        "参数摘要要能在剩余宽度里省略号收尾（不写 min-width:0 会把整行撑破）"
    )
    assert "background: var(--ui-green)" in _body_of(rules, ".tool-row.is-ok .tool-row-dot")
    assert "background: var(--ui-red)" in _body_of(rules, ".tool-row.is-bad .tool-row-dot")


def test_reply_rounds_have_visible_spacing() -> None:
    """段间要有**可感知**间距（又不是换个气泡那么夸张）。"""
    rules = _new_rules()
    body = _body_of(rules, ".reply-round + .reply-round")
    assert body, "缺 .reply-round 段间规则"
    gap = re.search(r"margin-top:\s*([0-9.]+)px", body)
    assert gap, f"段间间距要写明确的 px：{body}"
    assert 6 <= float(gap.group(1)) <= 16, f"段间间距 {gap.group(1)}px 不在可感知又不夸张的区间"
    assert "display: none" in _body_of(rules, ".reply-round:empty"), "空段不该占位"


def test_chat_head_layout_cannot_squeeze_the_title_or_the_usage() -> None:
    """标题右侧的用量：flex 一行 + 标题 min-width:0 + 用量 nowrap/收窄，两边都不被挤压。"""
    rules = _new_rules()
    head = _body_of(rules, ".chat-head")
    assert "display: flex" in head, ".chat-head 要改成一行 flex（否则用量会被挤到换行）"
    assert "min-width: 0" in _body_of(rules, ".chat-head h3"), (
        "标题不写 min-width:0，长标题会把右侧用量挤出容器"
    )
    usage = _body_of(rules, ".chat-head #session-usage")
    assert usage, "缺 #session-usage 的样式"
    assert "flex: 0 0 auto" in usage and "white-space: nowrap" in usage
    assert "margin: 0" in usage and "padding: 0" in usage, (
        "在场类 .hint 给的 12px 侧边距 / 8px 上下外边距必须清掉"
    )
    assert "text-overflow: ellipsis" in usage and "max-width" in usage


def test_notice_rule_exists() -> None:
    rules = _new_rules()
    assert _body_of(rules, ".msg.notice .body"), "缺 .notice 气泡样式"


def test_new_styles_avoid_undefined_tokens() -> None:
    offenders = []
    for sel, body in _new_rules():
        for token in LEGACY_TOKENS:
            if f"var({token})" in body:
                offenders.append((sel, token))
    assert not offenders, (
        f"新样式用了未定义的旧令牌 {offenders} —— "
        "改用 --ui-text-tertiary / --ui-stroke-secondary / --ui-bg-elevated 等真实令牌"
    )


def test_animation_is_local_only() -> None:
    """离线可用：动效只能是本地 CSS 关键帧，不能引外部资源。"""
    block = re.sub(r"/\*.*?\*/", "", STYLE_CSS, flags=re.S)
    assert "@keyframes msg-progress-spin" in block, "缺本地关键帧 msg-progress-spin"
    assert "http://" not in block and "https://" not in block


# ---------------------------------------------------------- 状态机真值表（node）


@pytest.fixture(scope="module")
def probe() -> dict[str, object]:
    """把 api.js 里的纯函数**切出来**在 node 里真跑（读的是产品文件，不是抄一份）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("没装 node，跳过加载圈/工具条目纯函数真值表")
    assert API_JS_PATH.is_file(), f"缺少 api.js：{API_JS_PATH}"
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(API_JS_PATH))}, 'utf8');
/* 切一段顶层函数：从 `function NAME` 起，到第一个顶格的 `}}` 为止。
   不用花括号配对 —— 函数体里有 `"{{}}"` 这种字符串字面量，配对会切歪。 */
function sliceFn(name) {{
  const i = src.indexOf('function ' + name);
  if (i < 0) throw new Error('找不到函数 ' + name);
  const j = src.indexOf('\\n}}', i);
  if (j < 0) throw new Error('函数 ' + name + ' 没有顶格收尾');
  return src.slice(i, j + 2);
}}
function sliceVar(name) {{
  const i = src.indexOf('var ' + name);
  if (i < 0) throw new Error('找不到变量 ' + name);
  const j = src.indexOf('\\n}};', i);
  return src.slice(i, j + 3);
}}
eval(
  sliceVar('TOOL_REF_LABELS') + '\\n' +
  sliceFn('turnSpinnerNext') + '\\n' +
  sliceFn('turnSpinnerText') + '\\n' +
  sliceFn('toolArgsSummary') + '\\n' +
  sliceFn('toolResultSummary') + '\\n' +
  sliceFn('fmtDuration') + '\\n' +
  sliceFn('toolLatencyText') + '\\n' +
  sliceFn('replyRoundNext') + '\\n'
);

/* 事件序列 → 加载圈可见性/文案。null 表示"这个事件不动状态"。 */
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

/* 分段：模拟 scoutEvent 的 chat.delta / chat.tool_call 序列，算出"一共几段、每段是什么"。
   roundClosed 由一次工具调用置真（见 app.js addToolCall），下一个增量才新开一段。
   注意这也是**后端契约**：runtime/loop.py 里一轮要么调工具、要么给最终答案（给答案就结束这一轮），
   所以"连着两段正文中间没有工具调用"在真实报文里不会出现（探针脚本按同一契约写）。 */
function rounds(seq) {{
  let closed = false, text = '', out = [];
  for (const step of seq) {{
    if (step === 'call') {{ closed = true; continue; }}
    const next = replyRoundNext(closed, step);
    if (next.isNew || out.length === 0) out.push('');
    closed = false;
    text = next.text;
    out[out.length - 1] = text;
  }}
  return out;
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
  toolText: turnSpinnerText(turnSpinnerNext(null, 'chat.tool_call', {{ tool: 'topic.build_map' }})),
  noToolName: turnSpinnerText(turnSpinnerNext(null, 'chat.tool_call', {{}})),
  idleHidden: turnSpinnerText(null),

  /* ---- 工具条目字段 ---- */
  args: {{
    squashed: toolArgsSummary('{{"query":\\n   "a    b"}}', 120),
    empty: toolArgsSummary('', 120),
    emptyObject: toolArgsSummary('{{}}', 120),
    truncated: toolArgsSummary('x'.repeat(300), 120),
  }},
  result: {{
    okWithRefs: toolResultSummary({{ ok: true, refs: {{ source_ids: ['a', 'b', 'c'] }} }}),
    okEmpty: toolResultSummary({{ ok: true, refs: {{}} }}),
    missingOk: toolResultSummary({{ refs: {{ source_ids: ['a'] }} }}),
    failed: toolResultSummary({{ ok: false, refs: {{ source_ids: ['a', 'b'] }} }}),
    unknownKey: toolResultSummary({{ refs: {{ paper_ids: ['a'] }} }}),
    mixed: toolResultSummary({{ refs: {{ source_ids: ['a', 'b', 'c'], route_ids: ['r'] }} }}),
  }},
  duration: {{
    ms: fmtDuration(12),
    second: fmtDuration(1000),
    long: fmtDuration(15000),
    negativeClamped: fmtDuration(-5),
  }},
  latency: {{
    backend: toolLatencyText({{ latency_ms: 2841 }}, 0, 99999),
    frontend: toolLatencyText({{}}, 1000, 1841),
    none: toolLatencyText({{}}, undefined, undefined),
    bogus: toolLatencyText({{ latency_ms: 'x' }}, undefined, undefined),
  }},
  /* 分段：3 轮正文，每轮都由**它自己的工具调用**收尾 → 三段（旧方案只会剩最后一段）。 */
  rounds: rounds(['call', '第一轮正文', 'call', '第二轮正文', 'call', '第三轮正文']),
  roundsNoTool: rounds(['只有一轮正文']),
  /* 契约的另一半（写下来免得后人误改）：分段判据是"工具调用收尾"，**不是**"文本重启"。
     一轮里没有工具调用 = 那一轮就是最终答案（整轮到此结束），所以真实报文里
     "连着两段正文中间没有工具调用"不会出现；真出现也只算同一段（后到的累计值接管）。 */
  roundsNoBoundary: rounds(['第一轮正文', '第二轮正文']),
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


def test_full_sequence_mapping(probe: dict[str, object]) -> None:
    steps = probe["full"]
    assert [step["visible"] for step in steps] == [True, True, True, True, False], (
        "turn.start/tool_call/tool_result/delta 都该显示，chat.done 后必须消失"
    )
    assert steps[0]["text"] == "正在思考…"
    assert steps[1]["text"] == "正在调用 research.search 工具", "调用中必须带工具名"
    assert steps[2]["text"] == "等待下一个动作…", "结果后换成等待文案"
    assert steps[3]["text"] == "正在生成回答…"
    assert steps[4]["text"] == "", "结束后文案必须清空"
    assert steps[4]["visible"] is False, "结束后必须消失（防永久转圈）"


def test_error_also_hides_the_loading_circle(probe: dict[str, object]) -> None:
    steps = probe["errorEnds"]
    assert [step["visible"] for step in steps] == [True, True, False]
    assert steps[-1]["visible"] is False


def test_unrelated_events_do_not_touch_state(probe: dict[str, object]) -> None:
    assert probe["unrelatedIsNull"] == [None, None, None], (
        "与加载圈无关的事件必须返回 null（不动状态）"
    )


def test_silence_keeps_the_tool_text(probe: dict[str, object]) -> None:
    assert probe["toolText"] == "正在调用 topic.build_map 工具"
    assert probe["noToolName"] == "正在调用工具…"
    assert probe["idleHidden"] == ""


def test_tool_args_summary_squashes_and_truncates(probe: dict[str, object]) -> None:
    args = probe["args"]
    assert args["squashed"] == '{"query": "a b"}', "参数摘要要压掉换行与连续空白"
    assert args["empty"] == "（无参数）"
    assert args["emptyObject"] == "（无参数）"
    assert len(args["truncated"]) == 121 and args["truncated"].endswith("…"), (
        "超长参数截到 120 字并加省略号"
    )


def test_tool_result_summary_reports_refs(probe: dict[str, object]) -> None:
    result = probe["result"]
    assert result["okWithRefs"] == "完成｜新增 来源 3 条"
    assert result["okEmpty"] == "完成"
    assert result["missingOk"] == "完成｜新增 来源 1 条", "缺 ok 字段按成功（与后端默认一致）"
    assert result["failed"] == "失败"
    assert result["unknownKey"] == "完成｜新增 paper_ids 1 条", "没登记的键要原样露出，别吞掉"
    assert result["mixed"] == "完成｜新增 来源 3 条 路线 1 条"


def test_duration_formatting(probe: dict[str, object]) -> None:
    duration = probe["duration"]
    assert duration["ms"] == "12 ms"
    assert duration["second"] == "1.0 s"
    assert duration["long"] == "15 s"
    assert duration["negativeClamped"] == "0 ms", "负数要夹到 0，不能显示 -5 ms"


def test_latency_prefers_backend_value_and_labels_frontend_clock(probe: dict[str, object]) -> None:
    """耗时优先用后端真实 latency_ms；没有才用前端计时，并且**标注"（前端计时）"**（不编数）。"""
    latency = probe["latency"]
    assert latency["backend"] == "2.8 s"
    assert latency["frontend"] == "841 ms（前端计时）"
    assert latency["none"] == "", "既没后端值也没计时就留空，不许编一个数"
    assert latency["bogus"] == "", "latency_ms 不是数字时不能当 0 用"


def test_reply_round_splits_per_turn(probe: dict[str, object]) -> None:
    """每轮正文各自成段：每轮都由它自己的工具调用收尾 → 三段，且内容不互盖。"""
    assert probe["rounds"] == ["第一轮正文", "第二轮正文", "第三轮正文"]
    assert probe["roundsNoTool"] == ["只有一轮正文"]
    assert probe["roundsNoBoundary"] == ["第二轮正文"], (
        "分段判据若是「文本重启」就会在这里多切一段 —— "
        "那条路会让收尾校正的全文变成上下重复的两段（真的踩点）"
    )


# ------------------------------------------------- 探针记录（真渲染的条数对账）

PROBE_JSON = Path(__file__).resolve().parents[2] / "run" / "ui_chat_probe" / "after-probe.json"


def test_recorded_probe_agrees_on_counts_and_geometry() -> None:
    """无头 Edge 真渲染量出来的条数与几何（探针产物；没有就跳过，缺机器也能过门禁）。

    这是"工具条目数 = 工具调用数""相邻栏位几何全程不动"这两条的**真渲染**证据：
    桩桥脚本里是 2 次 chat.tool_call / 3 轮 chat.delta，所以行数必须是 2、段数必须是 3，
    而 5 个栏位宽度全程一个数都不许变。
    """
    if not PROBE_JSON.is_file():
        pytest.skip(f"没有探针产物 {PROBE_JSON}（先跑 run/ui_chat_probe.py --tag after）")
    items = json.loads(PROBE_JSON.read_text(encoding="utf-8"))
    assert items, "探针产物是空的"
    done = [item for item in items if (item.get("probe") or {}).get("name") == "settled"]
    assert done, "探针里没有 settled 那一步（轮次没有收尾）"
    last = done[-1]["probe"]
    assert last["toolRowCount"] == 3, f"工具行数 ≠ 调用数：{last['toolRowCount']}"
    assert last["replyCount"] == 3, f"每轮正文没有各自留段：只剩 {last['replyCount']} 段"
    assert last["overlaySpinner"] is None, "旧的浮层加载圈节点还在"
    geoms = []
    for item in items:
        geom = (item.get("probe") or {}).get("geom") or {}
        geoms.append({key: geom.get(key) for key in BASELINE_GEOM})
    assert all(geom == geoms[0] for geom in geoms), f"轮次进行中栏位几何动了：{geoms}"
    assert geoms[0] == BASELINE_GEOM, f"栏位几何与改前实测不一致：{geoms[0]}"

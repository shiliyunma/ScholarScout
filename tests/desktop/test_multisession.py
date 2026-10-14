"""多会话并行（docs/20 第 5–6 步）：按会话分槽 + 事件按 session_id 路由 + 完成角标。

用户报的问题原文是「在其中一个会话运行时，我想切换到其他会话，会导致本次会话出现异常」。
根因一句话：界面只有**全局单飞闸 + 单一进行中槽**（`state.sending` / `state.pending`），
叠加事件信封不带 session_id —— 切到 B 以后 A 的事件会写进"此刻挂在显示里的那个气泡"。
所以这个文件钉的是一件事：**它不会退回去**。

覆盖：
1. `state.turns[session_id]` 每会话一个槽，`state.pending` / `state.sending` 一个引用都不许留；
2. `eventSessionKey` 拿不到 `session_id` 就返回 null → **不落任何槽**（宁可什么都不动，也不落错）；
3. `unreadFromDone` 真值表（不是当前会话才打角标；已经未读的保持未读）；
4. `turnSlot` / `sessionRunning` / `unreadCount` 的建/切/清行为（含"别的会话在跑不算"）；
5. 每会话一条 `.msg-stream`：**先建出这条流、再切 `.is-active`**（顺序反了首次打开会整块空白）；
6. 左栏「正在跑」标记与未读角标都从每会话槽现算，不靠列表重渲染；
7. `chat.done` 走 `finishTurn`（权威正文 + message_id 落定），`btn-stop` 只取消当前会话。

分工（刻意的）：**纯函数与状态机**在这里用 node 抽出来真跑（与 `test_markdown_js.py` /
`test_turn_spinner.py` 同一手法；没装 node 就跳过，不让缺 node 的机器变红）；
**真实 DOM / 栏位几何 / 截图**由 `run/ui_multisession_probe.py` 在无头 Edge 里量 ——
那里才是真渲染，这里钉的是"代码形状"。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ._ui import APP_JS, INDEX_HTML, STYLE_CSS, UI_DIR

PROBE_JS = Path(__file__).resolve().parent / "multisession_probe.js"

#: 旧方案的全局槽 —— 这两名字**一个引用都不许再出现**（出现就是"改了一半"）。
OLD_GLOBAL_SLOTS = ("state.pending", "state.sending")


def block(text: str, start: str, end: str) -> str:
    """取 start 锚点到它**之后**第一次出现的 end 锚点之间的那段代码。"""
    assert start in text, f"找不到起点锚：{start}"
    head = text.index(start)
    tail = text.index(end, head + len(start))
    assert tail > head, f"终点锚在起点锚之前：{start} → {end}"
    return text[head:tail]


@pytest.fixture(scope="module")
def probe_results() -> list[dict[str, object]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("没装 node，跳过纯函数/状态机那部分")
    assert PROBE_JS.is_file(), f"缺少用例集：{PROBE_JS}"
    completed = subprocess.run(
        [
            node,
            str(PROBE_JS),
            str(UI_DIR / "api.js"),
            str(UI_DIR / "app.js"),
        ],
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


def test_every_case_passes(probe_results: list[dict[str, object]]) -> None:
    failures = [f"{case['name']} —— {case['got']}" for case in probe_results if not case.get("ok")]
    assert not failures, "多会话纯函数/状态机用例失败：\n" + "\n".join(failures)


def test_probe_covers_the_risky_areas(probe_results: list[dict[str, object]]) -> None:
    """用例集本身别缩水：这四个面必须都有覆盖（少一条就等于少一道防线）。"""
    names = " ".join(str(case["name"]) for case in probe_results)
    for keyword in (
        "eventSessionKey",
        "unreadFromDone",
        "turnSlot",
        "sessionRunning",
        "unreadCount",
    ):
        assert keyword in names, f"用例集缺少对「{keyword}」的覆盖"
    assert len(probe_results) >= 18, f"用例太少（{len(probe_results)} 条），是不是被删了"


def test_old_global_slots_never_come_back() -> None:
    """**旧全局槽不回归**（这一条能拦住"改一半"）。

    `state.pending` / `state.sending` 是全局单槽与全局单飞闸的载体，正是"切会话就串/就发不出"
    的根。删了之后任何文件里再出现一次，就说明有人按老思路又加回来了 —— 那时它一定
    又是"落到此刻显示的那个气泡上"。逐文件扫，别只看 app.js。
    """
    offenders: list[str] = []
    for path in sorted(UI_DIR.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        for name in OLD_GLOBAL_SLOTS:
            if name in text:
                offenders.append(f"{path.name}: {name}")
    assert not offenders, "旧全局槽又回来了：" + "、".join(offenders)
    assert "state.turns" in APP_JS and "state.streams" in APP_JS, "每会话的槽/流没建在 state 上"


def test_events_are_routed_by_session_id_before_touching_the_ui() -> None:
    """事件先按 `session_id` 找槽，再动界面；拿不到槽就**什么都不做**。"""
    api_js = (UI_DIR / "api.js").read_text(encoding="utf-8")
    scout_event = api_js[api_js.index("window.scoutEvent = function") :]
    assert "const key = eventSessionKey(event.method, p);" in scout_event
    assert "const slot = key ? turnSlot(key, false) : null;" in scout_event
    gate = "if (!slot || !slot.trace) return;"
    assert gate in scout_event, '没有"取不到槽就掉头"那道门 —— 事件会掉进别人的流'
    # 门必须在所有写界面的调用之前
    for write in (
        "slot.trace.addToolCall(p)",
        "slot.trace.pushDelta(p.text",
        "turnSpinnerUpdate(slot,",
    ):
        assert scout_event.index(gate) < scout_event.index(write), f"{write} 跑在门之前了"
    # 抽背与标题不属 chat 轮次：必须在找槽那道门之前处理，否则永远收不到
    assert scout_event.index('"practice.delta"') < scout_event.index("const key = eventSessionKey(")


def test_chat_done_lands_the_authoritative_text_in_its_own_slot() -> None:
    """`chat.done` 单独走 `finishTurn(sessionId, params)`：权威正文 + message_id + 角标都在这落。"""
    api_js = (UI_DIR / "api.js").read_text(encoding="utf-8")
    scout_event = api_js[api_js.index("window.scoutEvent = function") :]
    assert 'if (event.method === "chat.done")' in scout_event
    assert "finishTurn(key, p);" in scout_event

    finish = block(APP_JS, "function finishTurn(", "async function send(")
    assert "function finishTurn(sessionId, params)" in finish, "收尾没按会话收（又变成全局收尾了）"
    assert "trace.setFinal(p.text);" in finish, "权威正文没落进这一轮的段落"
    assert 'slot.messageId = p.message_id || "";' in finish
    assert "slot.unread = unreadFromDone(sessionId, state.sessionId, slot.unread);" in finish
    assert "renderUnread();" in finish and "syncSendState();" in finish


def test_each_session_gets_its_own_stream_created_before_activating() -> None:
    """每会话一条 `.msg-stream`；**先建出这条流、再统一切 `.is-active`**。

    顺序反了就出真 bug（探针抓到过）：首次打开某个会话时它的节点是在切类循环**之后**
    才创建的 → 永远拿不到 `.is-active` → `.msg-stream{display:none}` 让整个消息区空白。
    """
    activate = block(APP_JS, "function activateStream(", "function turnSlot(")
    assert "const target = streamBox(key);" in activate
    assert activate.index("const target = streamBox(key);") < activate.index(
        '.classList.toggle("is-active"'
    ), "必须先建出这条流再切 is-active，否则首次打开的会话永远拿不到 is-active（消息区空白）"
    assert "return target;" in activate
    stream_box = block(APP_JS, "function streamBox(", "function activateStream(")
    assert 'node.className = "msg-stream";' in stream_box
    assert 'node.setAttribute("data-session", key);' in stream_box, (
        "流没带 data-session，探针/角标就没法按会话查"
    )


def test_msg_stream_css_shows_exactly_one_stream() -> None:
    for rule in (".msg-stream {", ".msg-stream.is-active {"):
        assert rule in STYLE_CSS, f"缺样式规则：{rule}"
    inactive = STYLE_CSS[STYLE_CSS.index(".msg-stream {") :]
    inactive = inactive[: inactive.index("}")]
    assert "display: none;" in inactive, "非当前会话的流必须不显示（否则两条流会叠在一起）"
    active = STYLE_CSS[STYLE_CSS.index(".msg-stream.is-active {") :]
    active = active[: active.index("}")]
    assert "display: flex;" in active
    # 类选择器上的 display 会压掉 UA 给 [hidden] 的 display:none —— 必须显式写
    assert ".unread-dot[hidden] {" in STYLE_CSS, "未读圆点没有 [hidden] 覆盖规则（藏不掉）"


def test_unread_badge_appears_and_is_cleared_by_switching() -> None:
    """角标：左栏行 + 顶栏计数；**切过去**（openSession）或点计数即清。"""
    assert 'id="unread-count"' in INDEX_HTML and "hidden" in block(
        INDEX_HTML, 'id="unread-count"', ">"
    ), "顶栏计数按钮要么不存在，要么不是初始隐藏"
    render = block(APP_JS, "function renderUnread(", "function renderMessage(")
    assert 'li.classList.toggle("is-unread", unread);' in render
    assert "dot.hidden = !unread;" in render
    assert "badge.hidden = count === 0;" in render
    open_fn = block(APP_JS, "async function openSession(", "async function refreshSide(")
    assert "if (slot) slot.unread = false;" in open_fn, "切过去没清角标"
    assert "renderUnread();" in open_fn
    # 点顶栏计数 → 跳到第一个有新回复的会话（跳过去即清）
    assert 'el("unread-count").addEventListener("click"' in APP_JS


def test_running_marker_is_computed_per_session() -> None:
    """「正在跑」是**每会话**状态：起跑与收尾都要现算，不能等列表重渲染。"""
    render_running = block(APP_JS, "function renderRunning(", "function unreadCount(")
    assert 'sessionRunning(li.getAttribute("data-session"))' in render_running
    sync = block(APP_JS, "function syncSendState(", "function renderRunning(")
    assert "renderRunning();" in sync, "起跑/收尾时没刷「正在跑」标记（会落后一整轮）"
    rendered = block(APP_JS, "function renderSessions(", "function startRename(")
    assert 'if (sessionRunning(item.session_id)) li.classList.add("is-running");' in rendered, (
        "列表重渲染时没带上「正在跑」（刷一次列表标记就没了）"
    )
    assert "sessionRunning(item.session_id)" in rendered and "state.sending" not in rendered


def test_send_gate_is_per_session_and_goes_async() -> None:
    """发送闸按会话判（A 在跑不该拦住 B）；请求走 `mode="async"` 立刻返回。"""
    send_fn = block(APP_JS, "async function send(text)", "async function loadSessions(")
    assert "if (sessionRunning(sessionId))" in send_fn, (
        "发送闸还是全局的（A 在跑会静默丢掉 B 的发送）"
    )
    assert 'mode: "async"' in send_fn
    assert 'notice("这个会话已经有一轮在跑' in send_fn, "被闸拦住时要如实说，别静默丢"
    assert "slot.trace = makePending(sessionId);" in send_fn, "进行中气泡没挂到本会话的流里"
    assert "const sessionId = state.sessionId;" in send_fn


def test_stop_only_cancels_the_current_session() -> None:
    stop_fn = block(APP_JS, "async function stopTurn(", "/* 说明：原先进回答末尾追加的")
    assert 'await scout.call("chat.cancel", { session_id: sessionId });' in stop_fn
    assert "else notice(note, sessionId);" in stop_fn, (
        "没有在跑的轮次时要落一条**本会话流**里的提示"
    )
    assert "syncSendState();" in stop_fn


def test_spinner_sweep_never_touches_another_session() -> None:
    """兜底清扫只扫**一条**流；退回"当前显示的流"会把别人正在转的圈摘掉（探针抓到过）。"""
    api_js = (UI_DIR / "api.js").read_text(encoding="utf-8")
    sweep = block(api_js, "function turnSpinnerSweep(", "function turnSpinnerReset(")
    assert "slot.trace.node.wrap" in sweep, "第一优先是这个会话自己的气泡"
    assert "state.streams[String(sessionId)]" in sweep, "第二优先是这个会话自己的流"
    assert "|| box;" not in sweep, "还在把整个消息区当兜底（会摘掉别的会话的圈）"
    reset = block(
        api_js,
        "function turnSpinnerReset(",
        "/* ---------------------------------------------- 事件路由",
    )
    assert "turnSpinnerSweep(slot, sessionId);" in reset

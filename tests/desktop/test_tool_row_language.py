"""聊天区工具行「说人话」+ 实时进度行（D1/D2 的界面侧修复）。

真后端 + 真 UI 评测里最糟的一段体验：问答轮**首字 47.6s**、整轮 51.7s，前 91% 的时间里
界面上只有 `research.search {"query": "…"}` / `source.fetch {"url": "…"}` 这类**原样英文工具调用**，
正文一个字都没到 —— 用户没法判断是"在干活"还是"卡死了"。

这一轮只改**聊天区的渲染**（不动模型调用、不动提示词），三件事：

1. 工具行说人话：`research.search` → 「正在检索：<query>」、「source.fetch」→「正在抓取：<域名>」…
   未登记的工具回退成「正在执行：<工具名>」，**任何分支都不许把参数 JSON 铺到界面上**；
2. 工具区顶部一行实时进度：`正在检索… 12s`（每秒走秒、工具结束停表）、多工具时「第 2 步」、
   超过 20s 追加一句「还在跑，可继续等或按停止」（只提示，不新增停止逻辑）；
3. 首个正文到达时进度行**淡出 + 折叠**，不是突然消失（突然消失像出错）。

分工：文案映射与时间行为（走秒/停表/20s/淡出）用 `tool_row_probe.js` 在 node 里**真跑产品源码**
（切函数 + 假时钟 + 最小 DOM shim，没装 node 就跳过）；样式与接线（`.tool-progress` 只用真令牌、
必须带过渡、进度行挂在工具区顶部）在这里读文本钉住 —— 这两块正是"看着改了、其实静默失效"的重灾区。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ._ui import STYLE_CSS, UI_DIR

PROBE_JS = Path(__file__).resolve().parent / "tool_row_probe.js"
APP_JS_PATH = UI_DIR / "app.js"
APP_ONLY = APP_JS_PATH.read_text(encoding="utf-8")

#: 本机真令牌（style.css 里有定义）。写了别的名字等于整条声明静默失效。
ALLOWED_TOKENS = {
    "--font-mono",
    "--ui-text-secondary",
    "--ui-text-tertiary",
    "--ui-text-quaternary",
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


def _progress_rules() -> list[tuple[str, str]]:
    return [(sel, body) for sel, body in _rules(STYLE_CSS) if ".tool-progress" in sel]


def _body_of(selector: str) -> str:
    return " ".join(body for sel, body in _progress_rules() if sel == selector)


@pytest.fixture(scope="module")
def probe_cases() -> list[dict[str, object]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("没装 node，跳过工具行人话/进度行的真跑用例")
    assert PROBE_JS.is_file(), f"缺少探针脚本：{PROBE_JS}"
    completed = subprocess.run(
        [node, str(PROBE_JS), str(APP_JS_PATH)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert completed.returncode == 0, f"node 跑挂了：{completed.stderr}"
    payload = (completed.stdout or "").strip()
    assert payload, "node 没有任何输出"
    return json.loads(payload)


def test_every_tool_row_case_passes(probe_cases: list[dict[str, object]]) -> None:
    failures = [f"{case['name']} —— {case['got']}" for case in probe_cases if not case.get("ok")]
    assert not failures, "工具行/进度行用例失败：\n" + "\n".join(failures)


def test_probe_still_covers_the_five_required_faces(probe_cases: list[dict[str, object]]) -> None:
    """用例集本身别缩水：任务点名的五件事必须各有用例在钉。"""
    names = " ".join(str(case["name"]) for case in probe_cases)
    for keyword in ("映射表", "未知工具", "不渲染原始 JSON", "进度行", "20s", "淡出"):
        assert keyword in names, f"用例集缺少对「{keyword}」的覆盖"
    assert len(probe_cases) >= 10, f"用例太少（{len(probe_cases)} 条），是不是被删了"


def test_tool_call_renders_human_text_and_never_the_raw_args() -> None:
    """接线层：工具行文案走 toolHumanParts，`args_preview` 不再直接进 DOM。"""
    call = APP_ONLY[
        APP_ONLY.index("addToolCall: function (params)") : APP_ONLY.index(
            "addToolResult: function (params)"
        )
    ]
    assert "const parts = toolHumanParts(tool, p.args_preview);" in call, "工具行没有走人话映射"
    assert "createRow(tool, parts)" in call
    assert "startToolProgress(parts)" in call, "一次工具调用要同时点亮进度行"
    assert "toolArgsSummary(" not in call, "还在把参数摘要原样塞进工具行（那就是一整屏英文）"
    assert "entry.row.title = tool" not in call

    create = APP_ONLY[APP_ONLY.index("function createRow(tool, parts)") :]
    create = create[: create.index("function paintToolProgress(")]
    assert "name.textContent = human.action;" in create, "工具名那一格要说人话"
    assert 'args.textContent = human.detail ? "：" + human.detail : "";' in create
    assert 'row.title = "工具：" + tool;' in create, "title 里只留工具名（参数 JSON 不上界面）"
    assert "data-tool" in create, "原始工具名要留在 data-tool 上供排查"


def test_progress_row_is_stacked_on_top_of_the_tool_area() -> None:
    """进度行挂在**工具区顶部**（第一条子节点），工具行按顺序排在它下面。"""
    start = APP_ONLY.index("function makePending(sessionId)")
    pending = APP_ONLY[start : APP_ONLY.index("function createRow(", start)]
    assert 'toolProgress.className = "tool-progress";' in pending
    assert 'toolProgress.setAttribute("role", "status");' in pending, "无障碍：进度要能被读屏念出来"
    assert 'toolProgress.setAttribute("aria-live", "polite");' in pending
    assert "toolProgress.hidden = true;" in pending, "还没调工具时不能占位"
    assert "toolFlow.appendChild(toolProgress);" in pending, "进度行必须落在工具区里"
    # 建在第一条工具行**之前** → 它天然是 tool-flow 的第一个子节点
    assert APP_ONLY.index("toolFlow.appendChild(toolProgress);", start) < APP_ONLY.index(
        "function createRow(", start
    )


def test_progress_timer_stops_on_result_and_text_arrival() -> None:
    """走秒只在等的时候跑：工具结束停表，正文到了淡出，收尾摘干净（三重收口）。"""
    result = APP_ONLY[APP_ONLY.index("addToolResult: function (params)") :]
    result = result[: result.index("pushDelta: function (text)")]
    assert "stopToolProgressTick();" in result, "工具结束没停表 → 秒数会一直跳"

    push = APP_ONLY[APP_ONLY.index("pushDelta: function (text)") :]
    push = push[: push.index("setFinal: function (text)")]
    assert "fadeToolProgress();" in push, "首个正文到达时要淡出进度行"

    finish = APP_ONLY[APP_ONLY.index("finish: function ()") :]
    finish = finish[: finish.index("node: node,")]
    assert "dropToolProgress();" in finish, "收尾必须把节点与定时器一起清干净"

    fade = APP_ONLY[APP_ONLY.index("function fadeToolProgress()") :]
    fade = fade[: fade.index("function dropToolProgress()")]
    assert 'toolProgress.classList.add("is-leaving");' in fade, "淡出要留出过渡的那一拍"
    assert "setTimeout(" in fade, "淡出结束后才摘节点（不是立即 remove）"
    assert "toolProgress.remove()" in fade


def test_progress_row_style_fades_collapses_and_uses_real_tokens() -> None:
    """样式：过渡 + 折叠 + 只用真令牌；[hidden] 要显式 display:none（类选择器会压掉 UA 规则）。"""
    rules = _progress_rules()
    assert rules, "style.css 里找不到任何 .tool-progress 规则"
    base = _body_of(".tool-progress")
    assert base, "缺 .tool-progress 规则"
    assert "display: flex" in base
    assert "font-family: var(--font-mono)" in base
    assert "font-size: 11px" in base
    assert "transition:" in base, "淡出/折叠没有过渡 → 会变成突然消失"
    assert "opacity 180ms" in base and "max-height 200ms" in base, (
        "透明度与高度要一起过渡：只淡不折叠会留一条空行，只折叠不淡出会跳一下"
    )

    hidden = _body_of(".tool-progress[hidden]")
    assert hidden and "display: none" in hidden

    leaving = _body_of(".tool-progress.is-leaving")
    assert leaving, "缺 .tool-progress.is-leaving 规则（淡出状态没样式＝看不出在退场）"
    assert "opacity: 0" in leaving and "max-height: 0" in leaving

    for name in (
        ".tool-progress-step",
        ".tool-progress-text",
        ".tool-progress-elapsed",
        ".tool-progress-hint",
    ):
        assert _body_of(name), f"缺 {name} 规则"

    offenders = []
    for sel, body in rules:
        for token in re.findall(r"var\((--[\w-]+)", body):
            if token not in ALLOWED_TOKENS:
                offenders.append((sel, token))
    assert not offenders, f"进度行用了未定义的令牌 {offenders}（整条声明会静默失效）"


def test_no_alert_and_no_extra_model_calls_in_the_chat_area() -> None:
    """硬规矩：本轮新增的渲染不许 alert；这一轮不新增模型调用、不改提示词。"""
    added = ""
    for name in ("toolDetailUsable", "toolHumanParts", "toolProgressText"):
        start = APP_ONLY.index(f"function {name}(")
        added += APP_ONLY[start : APP_ONLY.index("\n}", start)] + "\n"
    assert "alert(" not in added, "本轮新增的映射/进度函数里出现了 alert"
    for body in added.split("\n\n"):
        assert "scout.call" not in body, "这轮只改渲染：映射/进度函数不该调后端"

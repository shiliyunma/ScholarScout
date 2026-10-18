"""搜索意图选择卡（`chat.needs_choice`）的行为契约测试。

两层：

1. **node 真跑产品 js**（`choice_probe.js`）：把 `app.js` 里的 `choiceMessage` /
   `choiceCardKey` / `choiceCards` / `renderChoiceCard` / `answerChoice` 按名字切出来，
   用 `new Function` 把 `send` / `openSession` 等作为**形参**注入求值 —— 测的是产品文件
   里的那段源码，不是复刻版。断言"7 个选项齐、点击真的调了 send 且内容正确、连点只发一条、
   非当前会话的卡不串流、轮次在跑只提示"。
2. **跨层一句话对得上**：前端 `CHOICE_PHRASES` 的每一条正文，后端
   `search_intent._EXPLICIT_RULES` 都必须认出**同一个**偏好 —— 认不出就等于点选之后
   下一轮又走一遍模糊分类（白花一次调用，还可能被分类成别的类型）。

"真渲染 + 相邻栏位几何 + 截图"由 `run/ui_choice_probe.py`（无头 Edge + 桩桥）负责，
那是另一层证据；这里钉的是**纯函数与事件语义**，跑得快、进门槛。

没装 node 就跳过（本机有；别人机器上不该因为缺 node 而红）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "src" / "scholarscout" / "desktop" / "ui" / "app.js"
PROBE_JS = Path(__file__).resolve().parent / "choice_probe.js"


@pytest.fixture(scope="module")
def probe_results() -> list[dict[str, object]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("没装 node，跳过选择卡的 node 用例")
    assert APP_JS.is_file(), f"缺少界面源码：{APP_JS}"
    completed = subprocess.run(
        [node, str(PROBE_JS), str(APP_JS)],
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


def test_every_choice_case_passes(probe_results: list[dict[str, object]]) -> None:
    failures = [f"{case['name']} —— {case['got']}" for case in probe_results if not case.get("ok")]
    assert not failures, "选择卡用例失败：\n" + "\n".join(failures)


def test_probe_covers_the_risky_areas(probe_results: list[dict[str, object]]) -> None:
    """用例集本身别缩水：这几面各少一条就等于少一道防线。"""
    names = " ".join(str(case["name"]) for case in probe_results)
    for keyword in ("7 个选项", "桩 send 真被调到", "连点两次", "轮次还在跑", "非当前会话"):
        assert keyword in names, f"用例集缺少对「{keyword}」的覆盖"
    assert len(probe_results) >= 16, f"用例太少（{len(probe_results)} 条），是不是被删了"


def _choice_phrases() -> dict[str, str]:
    """从产品源码里抠出 `var CHOICE_PHRASES = {...}` 的键值对（只有 7 条简单字符串）。"""
    text = APP_JS.read_text(encoding="utf-8")
    match = re.search(r"var CHOICE_PHRASES = \{(.*?)\};", text, re.S)
    assert match, "app.js 里找不到 var CHOICE_PHRASES = {...};"
    pairs = dict(re.findall(r"(\w+):\s*\"([^\"]+)\"", match.group(1)))
    assert pairs, "CHOICE_PHRASES 解析出 0 条 —— 写法变了，先修这个解析再谈断言"
    return pairs


def test_every_option_has_a_phrase() -> None:
    """后端给的 7 类（六类 + none）在前端都必须有正文，否则某一类点下去发空串。"""
    from scholarscout.runtime import search_intent

    phrases = _choice_phrases()
    missing = [opt["type"] for opt in search_intent.options() if opt["type"] not in phrases]
    assert not missing, f"这些选项类型没有对应正文：{missing}"


def test_phrases_are_recognized_by_backend_rules() -> None:
    """点选发出去的那句话，后端显式规则必须认出**同一个**偏好（跨层契约）。"""
    from scholarscout.runtime import search_intent

    phrases = _choice_phrases()
    wrong: list[str] = []
    for preference, phrase in phrases.items():
        got = search_intent.explicit_preference(phrase)
        if got != preference:
            wrong.append(f"{phrase!r} → 期望 {preference}，后端认成 {got}")
    assert not wrong, "前端正文与后端显式规则不一致：\n" + "\n".join(wrong)

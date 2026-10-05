"""Markdown 渲染器的解析层测试（用 node 真跑 markdown.js 的纯函数）。

为什么不写成 Python 复刻：渲染器是 JS，复刻一遍等于测自己写的复刻版。
这里用 node 直接 require 产品文件（`ui/markdown.js` 末尾有 `module.exports`），
断言解析结果 —— 覆盖"看着没问题、实际静默出错"的那几类：

- 围栏代码里被当成正文解析（**粗体**、<b> 被吃掉）
- 没闭合的围栏（流式输出时必然出现）
- `javascript:` 伪协议被当成链接（点一下就注入）
- HTML 标签被当 HTML 解析（XSS）
- 推理块 `<thinking>` 没剥掉，整段推理被当回答渲染

没装 node 就跳过（本机有；CI/别人机器上不该因为缺 node 而红）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MARKDOWN_JS = ROOT / "src" / "scholarscout" / "desktop" / "ui" / "markdown.js"
PROBE_JS = Path(__file__).resolve().parent / "markdown_probe.js"


@pytest.fixture(scope="module")
def probe_results() -> list[dict[str, object]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("没装 node，跳过 markdown.js 的解析层测试")
    assert MARKDOWN_JS.is_file(), f"缺少渲染器文件：{MARKDOWN_JS}"
    completed = subprocess.run(
        [node, str(PROBE_JS), str(MARKDOWN_JS)],
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


def test_every_markdown_case_passes(probe_results: list[dict[str, object]]) -> None:
    failures = [f"{case['name']} —— {case['got']}" for case in probe_results if not case.get("ok")]
    assert not failures, "Markdown 解析用例失败：\n" + "\n".join(failures)


def test_probe_covers_the_risky_areas(probe_results: list[dict[str, object]]) -> None:
    """用例集本身别缩水：这些面必须都有覆盖（少一条就等于少一道防线）。"""
    names = " ".join(str(case["name"]) for case in probe_results)
    for keyword in ("围栏", "表格", "列表", "伪协议", "裸 URL", "HTML", "推理块", "isSafeUrl"):
        assert keyword in names, f"用例集缺少对「{keyword}」的覆盖"
    assert len(probe_results) >= 16, f"用例太少（{len(probe_results)} 条），是不是被删了"

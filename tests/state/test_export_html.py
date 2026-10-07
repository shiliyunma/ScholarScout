"""单页 HTML 导出（`state/export_html.py`）。

三件必须钉住的事（都是"看起来没问题、发出去才发现坏"的那类）：

1. **自包含**：不许有任何外部资源引用（`--check-ui` 的规矩延伸到导出件）——否则别人打开是白屏；
2. **内容不破坏结构**：正文里出现 `</script>`、超长文本、任意 Unicode 都不能把页面截断，
   所以正文走 base64 而**不是**直接塞进 `<script>`；
3. **与 Markdown 导出同源**：正文仍是 `build_markdown` 的结果（同一套路径打码、
   同样不导出 `role=tool` 的原始 JSON），且渲染器缺失时降级成 `<pre>` 而不是白屏。
"""

from __future__ import annotations

import base64
import re

import pytest

from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import dispatch
from scholarscout.state import export_html, sessions
from scholarscout.state.export_html import build_html, export_html_to_file


def _session_with(ctx, *messages: str) -> str:  # type: ignore[no-untyped-def]
    session_id = sessions.create_session(ctx.db, model="stub-model", title="导出测试")
    for index, text in enumerate(messages):
        sessions.append_message(
            ctx.db,
            session_id=session_id,
            role="user" if index % 2 == 0 else "assistant",
            content=text,
        )
    return session_id


def _payload(html: str) -> str:
    """把 base64 正文解回来（顺带证明它确实是 base64 而不是裸文本）。"""
    found = re.search(r'atob\("([A-Za-z0-9+/=]+)"\)', html)
    assert found, "找不到 base64 正文"
    return base64.b64decode(found.group(1)).decode("utf-8")


def test_html_is_self_contained(ctx) -> None:  # type: ignore[no-untyped-def]
    session_id = _session_with(ctx, "什么是 trigram 索引？", "三元组索引，短查询走 LIKE 兜底。")
    html = build_html(ctx.db, session_id)
    # 去掉正文载荷再查外部引用（正文里出现链接是内容，不算"外部资源"）
    shell = html.replace(re.search(r'atob\("([A-Za-z0-9+/=]+)"\)', html).group(1), "")  # type: ignore[union-attr]
    assert "http://" not in shell and "https://" not in shell
    assert "<link" not in shell and "src=" not in shell, "不许引用外部脚本/样式"
    assert "cdn" not in shell.lower()


def test_html_inlines_the_real_renderer(ctx) -> None:  # type: ignore[no-untyped-def]
    session_id = _session_with(ctx, "你好")
    html = build_html(ctx.db, session_id)
    assert "ScoutMarkdown" in html, "必须内联应用在用的渲染器（避免导出件与界面渲染漂移）"
    assert "renderInto" in html
    assert "prefers-color-scheme" in html, "深浅色跟随系统"


def test_payload_round_trips_to_the_markdown_body(ctx) -> None:  # type: ignore[no-untyped-def]
    session_id = _session_with(ctx, "**加粗**的提问", "回答里有 `代码` 与表格")
    html = build_html(ctx.db, session_id)
    body = _payload(html)
    assert "**加粗**的提问" in body
    # 与 Markdown 导出同源（同一份 build_markdown），但开头的 `# 标题` 去掉 —— 页头已经有标题了
    assert body.startswith("> 会话 `ses_"), "正文应从元信息行开始"
    assert "# 导出测试" not in body


def test_script_tag_in_content_cannot_break_the_page(ctx) -> None:  # type: ignore[no-untyped-def]
    """会话里出现 `</script>` 时文档结构必须完好（这是塞进 script 最经典的坑）。"""
    session_id = _session_with(ctx, "试试注入：</script><script>alert(1)</script>", "收到。")
    html = build_html(ctx.db, session_id)
    # 页面里只有模板自己的两个 <script>（渲染器 + 启动），内容里的 </script> 只在 base64 里
    assert html.count("<script>") == 2
    assert html.count("</script>") == 2
    assert "</script><script>alert(1)" not in html
    assert "</script><script>alert(1)" in _payload(html), "原样保留在正文里，只是不参与 HTML 解析"


def test_unjits_the_renderer_still_readable(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """渲染器读不到 → 降级成 `<pre>` 原样显示，不许白屏。"""
    monkeypatch.setattr(export_html, "_renderer_source", lambda: "")
    session_id = _session_with(ctx, "兜底也要能看")
    html = build_html(ctx.db, session_id)
    # 渲染器**定义**没进来（启动脚本里仍有 window.ScoutMarkdown 的探测，那是正常的）
    assert "var ScoutMarkdown" not in html
    assert 'document.createElement("pre")' in html
    assert "兜底也要能看" in _payload(html)


def test_paths_are_scrubbed_like_the_markdown_export(ctx) -> None:  # type: ignore[no-untyped-def]
    """导出件是给别人看的：本机绝对路径必须打码（与 Markdown 导出同一规则）。"""
    session_id = _session_with(ctx, "文件在 D:\\AI+教育毕设毕设\\ScholarScout\\data\\raw\\x.pdf")
    html = build_html(ctx.db, session_id)
    assert "ScholarScout\\data\\raw" not in _payload(html).replace("<path>", "")


def test_unknown_session_raises_keyerror(ctx) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(KeyError):
        build_html(ctx.db, "ses_nope")
    with pytest.raises(KeyError):
        export_html_to_file(ctx.db, "ses_nope")


def test_export_writes_a_file_and_reports_bytes(ctx, tmp_path) -> None:  # type: ignore[no-untyped-def]
    session_id = _session_with(ctx, "落盘测试")
    saved = export_html_to_file(ctx.db, session_id, out_dir=tmp_path)
    path = tmp_path / "exports" / saved["path"].split("/")[-1]
    assert path.exists()
    assert path.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert saved["bytes"] > 0 and saved["chars"] > 0


# ------------------------------------------------------------------ RPC


def test_rpc_export_html(ctx, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        export_html,
        "export_html_to_file",
        lambda db, sid, **kw: export_html_to_file(db, sid, out_dir=tmp_path),
    )
    session_id = _session_with(ctx, "RPC 导出")
    result = dispatch(ctx, "session.export", {"session_id": session_id, "fmt": "html"})
    assert result["fmt"] == "html"
    assert result["path"].endswith(".html")
    assert result["text"] == "", "HTML 不回正文（几十万字符，界面用不上）"


def test_rpc_export_defaults_to_markdown(ctx) -> None:  # type: ignore[no-untyped-def]
    session_id = _session_with(ctx, "默认格式")
    result = dispatch(ctx, "session.export", {"session_id": session_id})
    assert result["fmt"] == "md"
    assert result["text"].startswith("# 导出测试")


def test_rpc_export_rejects_unknown_format(ctx) -> None:  # type: ignore[no-untyped-def]
    session_id = _session_with(ctx, "格式校验")
    with pytest.raises(RpcError):
        dispatch(ctx, "session.export", {"session_id": session_id, "fmt": "pdf"})

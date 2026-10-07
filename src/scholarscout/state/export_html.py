"""把一次会话导出成**自带渲染器的单页 HTML**（只读、离线、双击可看）。

为什么要有第二个导出格式：Markdown 适合"再编辑"，但发给别人时常常被聊天软件吃掉格式，
或者对方根本没有 Markdown 阅读器。单页 HTML **不引用任何外部资源**（`--check-ui` 的规矩：
界面文件不许出现外链），双击就能看，断网也能看 —— 答辩演示和发给人都用得上。

口径（**与 Markdown 导出同源，不另写一份内容生成**）：正文仍然是 `build_markdown` 的结果
（同一套路径打码、同样"不导出 `role=tool` 原始 JSON"的规则），这里只是把它装进一个自包含的壳：
- 内联 `markdown.js`（**应用真正在用的那个渲染器**，避免"导出件与界面渲染不一致"的漂移）；
- 正文用 **base64** 承载：会话里出现 `</script>`、超长文本或任意 Unicode 都不会破坏文档结构
  （这是"塞进 `<script>` 里"最容易被忽略的坑：内容里一个 `</script>` 就能把页面截断）；
- 渲染器缺失时降级成 `<pre>` 原样显示 —— 宁可丑，不能不显示。

样式跟随系统 `prefers-color-scheme`：浅色近白、深色中性黑（与桌面端同一体系，不用深蓝）。
"""

from __future__ import annotations

import base64
from pathlib import Path
from string import Template
from typing import Any

from scholarscout import paths
from scholarscout.state.db import Database
from scholarscout.state.export_session import build_markdown
from scholarscout.state.migrate import now_iso
from scholarscout.state.sessions import get_session

#: 界面目录（内联渲染器的来源）
UI_DIR = Path(__file__).resolve().parents[1] / "desktop" / "ui"

_STYLE = """
:root {
  --bg: #fafafa; --fg: #1a1a1a; --muted: #6b7280; --line: #e5e7eb;
  --card: #ffffff; --accent: #2563eb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111111; --fg: #ededed; --muted: #9ca3af; --line: #2a2a2a;
    --card: #1a1a1a; --accent: #7aa2f7;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 20px 64px; background: var(--bg); color: var(--fg);
  font-family: -apple-system, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
  line-height: 1.7;
}
header.top, main#doc { max-width: 76ch; margin: 0 auto; }
header.top { margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--line); }
header.top h1 { font-size: 22px; margin: 0 0 8px; }
header.top .meta { color: var(--muted); font-size: 12px; margin: 0; }
.md-body h1, .md-body h2, .md-body h3 { line-height: 1.35; margin: 1.4em 0 0.5em; }
.md-body h1 { font-size: 20px; } .md-body h2 { font-size: 17px; } .md-body h3 { font-size: 15px; }
.md-body p { margin: 0.6em 0; }
.md-body code {
  background: var(--card); border: 1px solid var(--line); border-radius: 4px;
  padding: 1px 4px; font-size: 0.9em;
}
.md-body pre {
  background: var(--card); border: 1px solid var(--line); border-radius: 6px;
  padding: 10px 12px; overflow-x: auto;
}
.md-body pre code { border: 0; background: none; padding: 0; }
.md-body table { border-collapse: collapse; width: 100%; margin: 0.8em 0; font-size: 14px; }
.md-body th, .md-body td { border: 1px solid var(--line); padding: 6px 8px; text-align: left; }
.md-body th { background: var(--card); }
.md-body blockquote {
  margin: 0.8em 0; padding: 2px 12px; border-left: 3px solid var(--accent); color: var(--muted);
}
.md-body a { color: var(--accent); }
.md-body ul, .md-body ol { padding-left: 1.4em; }
footer.foot { max-width: 76ch; margin: 40px auto 0; color: var(--muted); font-size: 12px; }
"""

_TEMPLATE = Template(
    """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>$title｜ScholarScout 导出</title>
<style>$style</style>
</head>
<body>
<header class="top">
  <h1>$title</h1>
  <p class="meta">ScholarScout 会话导出｜模型 $model｜$stamp｜只读单页，无外部资源</p>
</header>
<main id="doc" class="md-body"></main>
<footer class="foot">由 ScholarScout 导出（本地存储，无云端上传）。渲染器内联，断网可看。</footer>
<script>$renderer</script>
<script>
(function () {
  var binary = atob("$payload");
  var bytes = new Uint8Array(binary.length);
  for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  var text = new TextDecoder("utf-8").decode(bytes);
  var target = document.getElementById("doc");
  if (window.ScoutMarkdown && ScoutMarkdown.renderInto) {
    ScoutMarkdown.renderInto(target, text);
  } else {
    // 渲染器没内联成功也要能看：原样显示，宁丑不缺
    var pre = document.createElement("pre");
    pre.textContent = text;
    target.appendChild(pre);
  }
})();
</script>
</body>
</html>
"""
)


def _renderer_source() -> str:
    """内联界面在用的渲染器；读不到就返回空串（模板会走 `<pre>` 降级）。"""
    path = UI_DIR / "markdown.js"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def build_html(db: Database, session_id: str, *, exports_dir: Any = None) -> str:
    """生成自包含的单页 HTML。会话不存在抛 `KeyError`。"""
    body = build_markdown(db, session_id, exports_dir=exports_dir)
    session = get_session(db, session_id)
    if session is None:
        raise KeyError(session_id)
    title = str(session.get("title") or "未命名会话")
    # 页头已经有大标题了：把正文开头那行 `# 标题` 去掉，否则一眼看到两个同样的大标题
    body = _strip_leading_heading(body)
    payload = base64.b64encode(body.encode("utf-8")).decode("ascii")
    return _TEMPLATE.substitute(
        title=_html_escape(title),
        model=_html_escape(str(session.get("model") or "未知")),
        stamp=now_iso()[:19].replace("T", " "),
        style=_STYLE,
        renderer=_renderer_source(),
        payload=payload,
    )


def _strip_leading_heading(body: str) -> str:
    """去掉正文最开头的 `# 标题`（页头已经有标题了，重复一次很显眼）。"""
    lines = body.splitlines()
    if lines and lines[0].startswith("# "):
        index = 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        return "\n".join(lines[index:])
    return body


def _html_escape(text: str) -> str:
    """标题与模型名要进 HTML（正文是 base64，不受影响）。"""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def export_html_to_file(db: Database, session_id: str, *, out_dir: Any = None) -> dict[str, Any]:
    """落一份 `.html` 到 `data/exports/`，返回 ``{path, chars}``（path 相对项目根）。"""
    html = build_html(db, session_id, exports_dir=out_dir)
    session = get_session(db, session_id) or {}
    base = out_dir or paths.data_dir()
    target_dir = Path(base) / "exports"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso()[:19].replace(":", "").replace("-", "").replace("T", "-")
    name = f"{stamp}-{(session.get('id') or session_id)}.html"
    path = target_dir / name
    path.write_text(html, encoding="utf-8")
    try:
        relative = str(path.relative_to(paths.home()))
    except ValueError:
        relative = str(path)
    return {"path": relative, "chars": len(html), "bytes": len(html.encode("utf-8"))}

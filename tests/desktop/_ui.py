"""前端资源的统一读取口（前端契约测试都用它）。

为什么要有这个文件：`app.js` 已经按业务拆成多个 js（api / sources / learning / board /
profile / app）。以前每个契约测试各自读 `app.js` 文本做漂移检查；拆分之后它们会**假通过** ——
断言的东西搬到别的文件了，读 app.js 依然不报错。所以读取口只留这一处。

断言「前端代码里有这段逻辑」时用 `APP_JS`（全部 js 的合并文本）。
"""

from __future__ import annotations

from pathlib import Path

UI_DIR = Path(__file__).resolve().parents[2] / "src" / "scholarscout" / "desktop" / "ui"

#: 契约测试读文本时的拼接顺序：**app.js 在前**。
#: 为什么不是 index.html 的加载顺序：这些测试用"两个锚点之间"取段来界定某段代码，
#: 而 app.js 保留了原单文件的主干顺序，把它排在最前，原来相邻的锚点在合并文本里仍然同序，
#: 取段才不会反序切空（踩过：profile.js 排在前面时 SETTINGS_SPEC 的取段就反了）。
JS_FILES = (
    "app.js",
    "api.js",
    "sources.js",
    "learning.js",
    "board.js",
    "profile.js",
    "markdown.js",
)


def _read(name: str) -> str:
    return (UI_DIR / name).read_text(encoding="utf-8")


#: 前端全部 js 的合并文本。断言「前端代码里有这段逻辑」用它，**不要**单独读 app.js
APP_JS = "\n".join(_read(name) for name in JS_FILES if (UI_DIR / name).is_file())

#: 单个文件的文本（要断言特定文件时才用）
MARKDOWN_JS = _read("markdown.js")
STYLE_CSS = _read("style.css")
INDEX_HTML = _read("index.html")

#: 需要路径而不是文本时用
INDEX_HTML_PATH = UI_DIR / "index.html"
STYLE_CSS_PATH = UI_DIR / "style.css"


def read_app_js() -> str:
    """兼容写法：等价于 APP_JS。"""
    return APP_JS


def between(text: str, start_anchor: str, end_anchor: str, window: int = 4000) -> str:
    """取"起点锚那段代码"。

    先找 end_anchor 在 start_anchor **之后** 的第一次出现，取到那里为止；
    如果 end_anchor 在它之前（前端按业务拆成多个 js 之后很常见，拼接顺序与原文件不同），
    就退化为从起点锚起最多 window 个字符。

    为什么不用 `text[index(a):index(b)]`：那要求两个锚点在原文件里相邻，
    拆分之后这个前提不再成立 —— 反序时切出的是空串，断言会莫名其妙地"没有这段代码"。
    """
    start = text.index(start_anchor)
    try:
        end = text.index(end_anchor, start)
    except ValueError:
        end = min(len(text), start + window)
    return text[start:end]

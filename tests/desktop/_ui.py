"""前端资源的统一读取口（前端契约测试都用它）。

为什么要有这个文件：`app.js` 已经按业务拆成多个 js（api / sources / learning / board /
profile / app）。以前每个契约测试各自读 `app.js` 文本做漂移检查；拆分之后它们会**假通过** ——
断言的东西搬到别的文件了，读 app.js 依然不报错。所以读取口只留这一处。

断言「前端代码里有这段逻辑」时用 `APP_JS`（全部 js 的合并文本）。
"""

from __future__ import annotations

from pathlib import Path

UI_DIR = Path(__file__).resolve().parents[2] / "src" / "scholarscout" / "desktop" / "ui"

#: 前端 js 的加载顺序（与 index.html 一致）
JS_FILES = (
    "markdown.js",
    "api.js",
    "sources.js",
    "learning.js",
    "board.js",
    "profile.js",
    "app.js",
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

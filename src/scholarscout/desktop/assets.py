"""桌面 UI 的静态资源校验（门槛脚本会跑）。

查三件事（都是真出事过会很难查的）：
1. 文件齐全、``index.html`` 真的引了 js/css；``ui/`` 下只有一份入口
2. **前端调用的方法必须真实存在**（拿 ``rpc.methods.method_names()`` 比对）——
   界面点了没反应的按钮，八成是方法名写错了
3. **不出现 http(s):// 外链**：答辩现场可能没网，界面必须离线可用
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: UI 目录下必须存在的文件
REQUIRED_FILES = (
    "index.html",
    "markdown.js",
    "api.js",
    "sources.js",
    "learning.js",
    "board.js",
    "profile.js",
    "app.js",
    "style.css",
)

#: 前端 JS（按加载顺序）。app.js 拆成多层后，校验必须扫**全部** js ——
#: 只读 app.js 会假通过：搬到别的文件里的方法名写错、外链、忘用渲染器都查不出来。
JS_FILES = (
    "markdown.js",
    "api.js",
    "sources.js",
    "learning.js",
    "board.js",
    "profile.js",
    "app.js",
)

#: 前端调用后端的写法：``scout.call("archive.list", {...})``
_CALL_RE = re.compile(r"""scout\s*\.\s*call\(\s*['"]([A-Za-z][A-Za-z0-9_.]*)['"]""")
_EXTERNAL_RE = re.compile(r"https?://", re.IGNORECASE)


def ui_dir() -> Path:
    return Path(__file__).resolve().parent / "ui"


def called_methods(text: str) -> set[str]:
    return set(_CALL_RE.findall(text or ""))


def verify_ui(ui: Path | None = None) -> list[str]:
    """返回问题清单（空列表 = 通过）。"""
    from scholarscout.rpc.methods import method_names

    base = ui or ui_dir()
    problems: list[str] = []
    for name in REQUIRED_FILES:
        if not (base / name).is_file():
            problems.append(f"缺少 UI 文件：{name}")
    if problems:
        return problems

    html = (base / "index.html").read_text(encoding="utf-8")
    css = (base / "style.css").read_text(encoding="utf-8")

    # 全部前端 js 的合并文本：方法名、外链、渲染器用法都按"全项目"判
    all_js = {
        name: (base / name).read_text(encoding="utf-8")
        for name in JS_FILES
        if (base / name).is_file()
    }
    # 应用侧的 js（不含渲染器自身）：判断"有没有用渲染器""调了哪些方法"只看这些，
    # 否则 markdown.js 自己定义了 ScoutMarkdown，这条守卫会变成永远通过。
    app_side = [text for name, text in all_js.items() if name != "markdown.js"]
    app_js_all = "\n".join(app_side)

    for name, text in [("index.html", html), *all_js.items(), ("style.css", css)]:
        if _EXTERNAL_RE.search(text):
            problems.append(f"{name} 引用了外网资源（界面必须离线可用）")

    if "app.js" not in html:
        problems.append("index.html 没有引入 app.js")
    if "style.css" not in html:
        problems.append("index.html 没有引入 style.css")
    if "markdown.js" not in html:
        problems.append("index.html 没有引入 markdown.js")
    if "markdown.js" in html and html.index("markdown.js") > html.index("app.js"):
        problems.append("markdown.js 必须在 app.js 之前引入（app.js 会用 ScoutMarkdown）")
    # 拆成多层后新增的两条：每个 js 都要被引到（否则拆出来的文件成了死文件），
    # 且除 markdown.js 外都要排在 markdown.js 之后
    for name in JS_FILES:
        if (base / name).is_file() and name not in html:
            problems.append(f"index.html 没有引入 {name}")
    if "markdown.js" in html:
        md_at = html.index("markdown.js")
        for name in JS_FILES:
            if name != "markdown.js" and f'"{name}' in html and html.index(f'"{name}') < md_at:
                problems.append(f"{name} 必须排在 markdown.js 之后（它要用渲染器）")
    if "ScoutMarkdown" not in app_js_all:
        problems.append("前端没有使用 ScoutMarkdown（回答正文应当是渲染过的 Markdown）")

    called = called_methods(app_js_all)
    unknown = sorted(called - set(method_names()))
    if unknown:
        problems.append(f"前端调了不存在的方法：{', '.join(unknown)}")
    if not called:
        problems.append("前端没调用任何后端方法（是不是忘了 scout.call？）")

    extra_entries = sorted(p.name for p in base.glob("*.html") if p.name != "index.html")
    if extra_entries:
        problems.append(f"ui/ 下多余的入口页：{', '.join(extra_entries)}")

    return problems


def main() -> int:
    problems = verify_ui()
    if problems:
        for problem in problems:
            print(f"[FAIL] {problem}")
        return 1
    print("[OK] UI 资源：文件齐全、方法名都存在、无外链")
    return 0


if __name__ == "__main__":
    sys.exit(main())

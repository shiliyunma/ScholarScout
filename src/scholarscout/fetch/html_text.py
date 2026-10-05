"""HTML → 正文（stdlib HTMLParser，不引解析库）。

策略：
1. 丢掉 script / style / noscript / svg / template / nav / footer / aside 里的文字
2. 同时收集「全页文本」与「主内容文本」（main / article 容器内）
3. 主内容够长（>= 200 字）就用它，否则退回全页 —— 官方文档页与博客都能覆盖

⚠️ 本模块解析第三方输入，属于关键路径（docs/03 §5）：不执行任何脚本、不做正则回溯爆炸的匹配。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

#: 这些标签里的文字不算正文
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "nav", "footer", "aside", "form"}
#: 这些标签通常是主内容容器
MAIN_TAGS = {"main", "article"}
#: 主内容少于这个字符数就退回全页
MIN_MAIN_CHARS = 200
_BLOCK_TAGS = {"p", "div", "section", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
_WS = re.compile(r"[ \t\u00a0]+")


@dataclass(frozen=True)
class HtmlDocument:
    title: str
    text: str
    lang: str | None = None
    main_chars: int = 0

    @property
    def used_main(self) -> bool:
        return self.main_chars >= MIN_MAIN_CHARS


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.all_parts: list[str] = []
        self.main_parts: list[str] = []
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.lang: str | None = None
        self._skip_depth = 0
        self._main_depth = 0
        self._in_title = False
        self._in_h1 = False

    # ---------------------------------------------------------------- 标签

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name == "html" and self.lang is None:
            for key, value in attrs:
                if key.lower() == "lang" and value:
                    self.lang = value.strip()
        if name in SKIP_TAGS:
            self._skip_depth += 1
            return
        if name in MAIN_TAGS:
            self._main_depth += 1
        if name == "title":
            self._in_title = True
        if name == "h1":
            self._in_h1 = True
        # 只在 <br> 上补换行；块级元素的换行在结束标签处补（避免空行翻倍）
        if name == "br":
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if name in MAIN_TAGS:
            self._main_depth = max(0, self._main_depth - 1)
        if name == "title":
            self._in_title = False
        if name == "h1":
            self._in_h1 = False
        if name in _BLOCK_TAGS:
            self._append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_h1:
            self.h1_parts.append(data)
        self._append(data)

    # ---------------------------------------------------------------- 收集

    def _append(self, data: str) -> None:
        if self._skip_depth > 0 or not data:
            return
        self.all_parts.append(data)
        if self._main_depth > 0:
            self.main_parts.append(data)


def _normalize(parts: list[str]) -> str:
    text = "".join(parts)
    text = _WS.sub(" ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_html(html: str) -> HtmlDocument:
    parser = _Extractor()
    parser.feed(html)
    parser.close()

    all_text = _normalize(parser.all_parts)
    main_text = _normalize(parser.main_parts)
    title = _normalize(parser.title_parts) or _normalize(parser.h1_parts)
    use_main = len(main_text) >= MIN_MAIN_CHARS
    return HtmlDocument(
        title=title,
        text=main_text if use_main else all_text,
        lang=parser.lang,
        main_chars=len(main_text),
    )

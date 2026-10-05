"""HTML 正文抽取契约：去脚本/导航、主内容优先、标题、语言。"""

from __future__ import annotations

from scholarscout.fetch.html_text import MIN_MAIN_CHARS, extract_html

PAGE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <title>教育智能体官方文档</title>
  <style>body{color:red}</style>
  <script>var x = "<p>这段不该出现</p>";</script>
</head>
<body>
  <nav><a href="/a">导航链接</a></nav>
  <main>
    <h1>快速开始</h1>
    <p>先安装依赖。</p>
    <p>然后运行命令。</p>
  </main>
  <footer>版权所有</footer>
</body>
</html>
"""

LONG_MAIN = "<main>" + "".join(f"<p>正文第{i}段。</p>" for i in range(40)) + "</main>"


def test_extracts_title_and_lang() -> None:
    doc = extract_html(PAGE)
    assert doc.title == "教育智能体官方文档"
    assert doc.lang == "zh-CN"


def test_skips_script_style_nav_footer() -> None:
    doc = extract_html(PAGE)
    assert "这段不该出现" not in doc.text
    assert "color:red" not in doc.text
    assert "导航链接" not in doc.text
    assert "版权所有" not in doc.text


def test_short_main_falls_back_to_full_page() -> None:
    doc = extract_html(PAGE)
    assert doc.used_main is False
    assert "先安装依赖" in doc.text


def test_long_main_is_preferred() -> None:
    html = "<html><body><div>页面顶部的废话" + "顶" * 300 + f"</div>{LONG_MAIN}</body></html>"
    doc = extract_html(html)
    assert doc.used_main is True
    assert doc.main_chars >= MIN_MAIN_CHARS
    assert "页面顶部的废话" not in doc.text
    assert "正文第39段" in doc.text


def test_block_tags_become_newlines() -> None:
    doc = extract_html("<html><body><p>第一段</p><p>第二段</p></body></html>")
    assert doc.text == "第一段\n第二段"


def test_title_falls_back_to_h1() -> None:
    doc = extract_html("<html><body><h1>只有 H1 标题</h1><p>内容</p></body></html>")
    assert doc.title == "只有 H1 标题"


def test_empty_html() -> None:
    doc = extract_html("")
    assert doc.text == "" and doc.title == ""

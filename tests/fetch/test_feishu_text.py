"""`to_feishu_text` 的桩测：**纯文字化** + **幂等**（`f(f(x)) == f(x)`）。

为什么要专门一个"幂等表"：出站文本在真实链路里会被剥两遍 ——
`channels/feishu.py` 先拼好回执/回答的文案，`fetch/feishu_send.send_text` 再统一剥一次；
`channel.send_file` 的 `reply` 也是同一条路。所以 f 的输出**必须**是 f 自己的不动点。

原实现不幂等的真因（已修）：围栏行删掉、代码内容原样留下 —— 保护跟着标记一起消失，
第二次调用就把 `# 注释` 当标题、`| a | b |` 当表格、`**x**` 当粗体继续剥。
修法是让"这段是代码"的边界**跟着输出走**（`CODE_OPEN` / `CODE_CLOSE`），而不是靠多剥一次掩盖。
"""

from __future__ import annotations

import pytest

from scholarscout.fetch.feishu_send import CODE_CLOSE, CODE_OPEN, to_feishu_text

#: 幂等表：强/斜体、标题、代码块、表格、列表、引用、链接、行内代码、水平线、混合、CRLF、空串
IDEMPOTENT_CASES: dict[str, str] = {
    "空串": "",
    "纯空白": "   \n\t\n  ",
    "强体星号": "这是 **粗体** 与 **另一个**。",
    "强体下划线": "__强__ 与 _斜_ 与 ___both___",
    "三元星号": "***加粗斜体*** 与 *斜*",
    "标题": "## 标题二\n### 三级\n正文",
    "正文里的井号": "#没有空格的标题\n# 有空格",
    "无序列表": "- 一\n* 二\n+ 三",
    "有序列表": "1. 一\n2. 二\n10. 十",
    "嵌套列表": "  - 子项\n    - 孙项\n1. 混排",
    "表格": "| 名字 | 值 |\n|---|---|\n| a | 1 |\n| b | 2 |",
    "管道散文": "a | b | c",
    "引用": "> 一层",
    "双层引用": ">> 双层引用",
    "空格式双层引用": "> > 也是这样",
    "水平线": "---\n***\n___\n正文",
    "链接": "看 [文档](https://example.com/a) 与 ![图](https://example.com/a.png)",
    "行内代码": "调用 `f(x)` 与 ``双反引号`` 与 ```三反引号```",
    "代码块": "```python\n# 注释\nx = 1\n- 不是列表\n| a | b |\n**不是粗体**\n> 不是引用\n```",
    "波浪号围栏": "~~~\n# 注释\n~~~",
    "未闭合围栏": "说明\n```\n# 注释\n- 不是列表",
    "代码块夹在正文里": "前\n```\ncode = '*'\n```\n后",
    "代码区空行": "```\na\n\n\nb\n```",
    "混合长样例": (
        "## 标题\n\n**粗** 与 *斜* 和 `code`\n\n"
        "- 列表项\n  - 子项\n\n"
        "| h | v |\n|---|---|\n| 1 | 2 |\n\n"
        "> 引用\n\n```bash\n# c\n- dash\n| p | q |\n**bold**\n```\n\n1. 有序\n"
    ),
    "CRLF": "# 标题\r\n\r\n**粗**\r\n```\r\n# code\r\n```\r\n",
    "连续空行": "前\n\n\n\n后",
    "缩进代码": "    缩进 4 格\n\tTab 开头",
}


@pytest.mark.parametrize("name", list(IDEMPOTENT_CASES), ids=list(IDEMPOTENT_CASES))
def test_to_feishu_text_is_idempotent(name: str) -> None:
    """`f(f(x)) == f(x)`：第二次剥不许再动一个字节（这是会真触发两次的路径）。"""
    source = IDEMPOTENT_CASES[name]
    once = to_feishu_text(source)
    twice = to_feishu_text(once)
    assert once == twice, f"「{name}」不幂等：\n  f(x)  ={once!r}\n  f(f(x))={twice!r}"


# ------------------------------------------------------------------ 各形态的期望输出


def test_headings_bold_italic_and_inline_code_are_stripped() -> None:
    assert to_feishu_text("## 标题\n\n**粗** 与 *斜* 和 `code`") == "标题\n\n粗 与 斜 和 code"


def test_bold_wrappers_do_not_leave_stray_asterisks() -> None:
    assert to_feishu_text("**a** 和 **b**") == "a 和 b"
    assert to_feishu_text("__强__ 与 _斜_") == "强 与 斜"


def test_lists_use_middle_dot_and_keep_ordered_numbers() -> None:
    # 无序 → `·`；有序保留（飞书里 `1.` 本来就看得懂）
    assert to_feishu_text("- 一\n- 二\n1. 三") == "· 一\n· 二\n1. 三"


def test_table_pipes_become_double_space_and_separator_row_is_dropped() -> None:
    out = to_feishu_text("| 名字 | 值 |\n|---|---|\n| a | 1 |")
    assert out == "名字  值\n\na  1"
    assert "|" not in out and "---" not in out


def test_links_keep_the_url_because_plain_text_has_no_hyperlink() -> None:
    assert to_feishu_text("看 [文档](https://example.com/a)") == "看 文档 https://example.com/a"
    # 图片也是同一套语法：留 alt + 地址
    assert to_feishu_text("![图](https://example.com/y.png)") == "图 https://example.com/y.png"


def test_quote_depth_is_stripped_in_one_pass() -> None:
    """`>>` 只剥一层会剩 `> x`，第二次调用再剥一次 —— 那正是原先不幂等的一种。"""
    assert to_feishu_text(">> 双层引用") == "双层引用"
    assert to_feishu_text("> 一层引用") == "一层引用"


def test_horizontal_rule_is_dropped() -> None:
    assert to_feishu_text("---\n正文") == "\n正文"


def test_consecutive_blank_lines_collapse_to_one() -> None:
    assert to_feishu_text("前\n\n\n\n后") == "前\n\n后"


def test_crlf_is_normalized() -> None:
    assert to_feishu_text("a\r\nb\rc") == "a\nb\nc"


# ------------------------------------------------------------------ 代码块：逐字节保留


CODE_SAMPLE = "```python\n# 注释\nx = 1\n\n- 不是列表\n| a | b |\n**不是粗体**\n  > 不是引用\n```"


def test_code_block_content_is_preserved_byte_for_byte() -> None:
    out = to_feishu_text(CODE_SAMPLE)
    lines = out.split("\n")
    assert lines[0] == CODE_OPEN and lines[-1] == CODE_CLOSE
    # 围栏里的 7 行**一个字节都没变**（含 `#`、`*`、`|`、`-`、前导空格与空行）
    assert lines[1:-1] == [
        "# 注释",
        "x = 1",
        "",
        "- 不是列表",
        "| a | b |",
        "**不是粗体**",
        "  > 不是引用",
    ]


def test_unclosed_fence_is_closed_so_the_next_pass_cannot_eat_the_code() -> None:
    out = to_feishu_text("说明\n```\n# 注释")
    assert out == f"说明\n{CODE_OPEN}\n# 注释\n{CODE_CLOSE}"
    assert to_feishu_text(out) == out


def test_markers_stay_balanced_for_every_case() -> None:
    for source in IDEMPOTENT_CASES.values():
        out = to_feishu_text(source)
        assert out.count(CODE_OPEN) == out.count(CODE_CLOSE), source


def test_code_block_blank_lines_are_kept() -> None:
    """代码区里的空行是代码（分隔两个函数），不能被"压空行"压掉。"""
    out = to_feishu_text("```\na\n\n\nb\n```")
    assert out == f"{CODE_OPEN}\na\n\n\nb\n{CODE_CLOSE}"

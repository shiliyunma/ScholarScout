"""``file.list`` / ``file.read`` 工具层契约：**只读 + 路径牢笼 + 复用现有抽取器**。

为什么这一层必须单独测（``fetch/`` 的抽取器各有自己的用例）：``file_tools`` 是唯一决定
"模型能读到什么、读不到时看到什么"的地方，而它有两处最容易静默出错：

1. **牢笼**：判据是 ``realpath`` 之后的真实路径。``..``、绝对路径、指向外面的符号链接这
   三种写法在字符串上都可能"看着清白" —— 少拦一种，模型就能读到 ``data/`` 之外的任意文件，
   而且**不会报错**（读成功了，没人会发现）。所以三种越界各一条用例，都断言
   ``data.kind == outside_allowed_root``；
2. **错误形状**：不支持的类型、文件不存在都必须是 ``ok=false`` + 可读 ``kind``，
   而不是冒泡成异常 / 500 —— 主循环会把异常记成"工具坏了"，模型就学不到"该换什么办法"。

所有落盘都在 ``tmp_path`` 下（``redirect_home`` 把 ``SCHOLARSCOUT_HOME`` 指过去），
PDF 用 pymupdf 现造、docx 用标准库 zip 现造（``fetch/office.py`` 本来就只解 zip + XML，
不依赖 python-docx），一个真实用户文件都不碰。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scholarscout.config import Config
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state.db import Database
from scholarscout.tools.file_tools import file_list, file_read

from ..conftest import redirect_home

#: docx 的最小可用形状：``fetch/office.docx_text`` 只读 ``word/document.xml`` 里的 ``w:p``/``w:t``
_DOCX_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>第一段：教育智能体的检索增强</w:t></w:r></w:p>
    <w:p><w:r><w:t>第二段：在 MATH 上把准确率从 42% 提到 58%</w:t></w:r></w:p>
  </w:body>
</w:document>
"""


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """落盘根重定向到 tmp：``paths.data_dir()`` 于是等于 ``tmp_path/data``（牢笼的唯一允许根之一）。"""
    return redirect_home(monkeypatch, tmp_path)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """会话工作目录：与 ``py.exec`` 的 workdir 同一个概念（``ctx.extra["workdir"]``）。

    建成 ``data/exports`` 而不是随手一个目录，是为了让"牢笼外"有一个**真实存在**的
    上级文件（``tmp_path/secret.md``）可以指向 —— 越界用例必须证明"拦的是路径，不是不存在"。
    """
    target = tmp_path / "data" / "exports"
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def ctx(db: Database, config: Config, workdir: Path) -> ToolContext:
    """工具层上下文（与真实调用同一形状，只多一个会话工作目录）。"""
    return ToolContext(
        db=db, config=config, session_id="ses_file_tools", extra={"workdir": str(workdir)}
    )


def _write(path: Path, text: str, *, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


# --------------------------------------------------------------------------- #
# 1. 文本：带行号 + total_lines
# --------------------------------------------------------------------------- #


def test_read_text_returns_numbered_lines_and_total(ctx: ToolContext, workdir: Path) -> None:
    """文本文件回**带行号**的每一行，``total_lines`` 是文件总行数（不是这一页的行数）。

    行号必须是**文件里的绝对行号**：提示词要求模型引用原文时写行号（``FILES_RULE`` 第 2 条），
    行号错位等于让用户核对不到原文。分页读取时行号也要从头连续（见分页那条用例）。
    """
    _write(workdir / "notes.md", "第一行\n第二行\n第三行\n")

    result = file_read(ctx, path="notes.md")

    assert result["ok"] is True, result
    data = result["data"]
    assert data["kind"] == "text"
    assert data["total_lines"] == 3
    assert data["count"] == 3
    assert data["lines"] == [
        {"n": 1, "text": "第一行"},
        {"n": 2, "text": "第二行"},
        {"n": 3, "text": "第三行"},
    ]
    assert data["truncated"] is False
    assert data["next_offset"] is None


# --------------------------------------------------------------------------- #
# 2. PDF：走 pymupdf 抽文字层
# --------------------------------------------------------------------------- #


def test_read_pdf_extracts_text_layer(ctx: ToolContext, workdir: Path) -> None:
    """PDF 走 ``fetch/pdf_text.py`` 抽文字层（不是把字节硬解成乱码）。

    这条用例证明 ``file.read`` 真的**复用了**抓取侧那个抽取器：引擎名与正文都来自它。
    """
    pymupdf = pytest.importorskip("pymupdf")  # 硬依赖（pyproject）；缺了也不该整份文件红
    document = pymupdf.open()
    try:
        page = document.new_page()
        page.insert_text((72, 72), "ScholarScout pdf marker 42", fontsize=12)
        target = workdir / "paper.pdf"
        document.save(str(target))
    finally:
        document.close()

    result = file_read(ctx, path="paper.pdf")

    assert result["ok"] is True, result
    data = result["data"]
    assert data["kind"] == "pdf"
    assert data["engine"] == "local"
    assert "ScholarScout pdf marker 42" in "\n".join(line["text"] for line in data["lines"])


# --------------------------------------------------------------------------- #
# 3. docx：走 Office 抽取器（标准库 zip + XML）
# --------------------------------------------------------------------------- #


def test_read_docx_extracts_paragraphs(ctx: ToolContext, workdir: Path) -> None:
    """docx 按段落抽字：两段正文都要在，一行一段。"""
    target = workdir / "report.docx"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("word/document.xml", _DOCX_XML)

    result = file_read(ctx, path="report.docx")

    assert result["ok"] is True, result
    data = result["data"]
    assert data["kind"] == "office"
    texts = [line["text"] for line in data["lines"]]
    assert texts == ["第一段：教育智能体的检索增强", "第二段：在 MATH 上把准确率从 42% 提到 58%"]
    assert data["total_lines"] == 2


# --------------------------------------------------------------------------- #
# 4. 分页：offset/limit 与 truncated / next_offset
# --------------------------------------------------------------------------- #


def test_read_pagination_marks_truncated_and_gives_next_offset(
    ctx: ToolContext, workdir: Path
) -> None:
    """``offset=3 & limit=4`` 只回第 3–6 行，标 ``truncated`` 并给 ``next_offset=7``。

    三件事一起钉：行号从**文件**算（不是从 1）、``total_lines`` 是全文行数、续读位置正确 ——
    少任何一个，模型都会把截断处当文件结尾，然后凭半截内容下结论（``FILES_RULE`` 第 3 条）。
    """
    _write(workdir / "long.txt", "\n".join(f"line-{index}" for index in range(1, 11)) + "\n")

    result = file_read(ctx, path="long.txt", offset=3, limit=4)

    assert result["ok"] is True, result
    data = result["data"]
    assert data["total_lines"] == 10
    assert [line["n"] for line in data["lines"]] == [3, 4, 5, 6]
    assert data["count"] == 4
    assert data["truncated"] is True
    assert data["truncated_by"] == "limit"
    assert data["next_offset"] == 7

    # 续读到尾之后必须**不再**标 truncated（否则模型会一直翻页翻下去）
    tail = file_read(ctx, path="long.txt", offset=data["next_offset"], limit=100)
    assert tail["ok"] is True
    assert tail["data"]["truncated"] is False
    assert tail["data"]["next_offset"] is None
    assert tail["data"]["lines"][-1] == {"n": 10, "text": "line-10"}


# --------------------------------------------------------------------------- #
# 5–7. 三种越界：`..` / 绝对路径 / 符号链接
# --------------------------------------------------------------------------- #


def test_parent_traversal_is_blocked(ctx: ToolContext, tmp_path: Path) -> None:
    """``../../secret.md`` 被拦：``..`` 在规范化后落在牢笼外，**不打开**。

    文件是真实存在且可读的 —— 这一条证明拦的是路径判据，而不是"读不到所以报错"。
    （一级 ``..`` 会让到 ``data/`` 里，那是**允许**的：牢笼是 ``data/`` 与会话工作目录，
    所以探到牢笼外得写两级 —— 这本身就是"按真实路径判定"的直接后果。）
    """
    secret = _write(tmp_path / "secret.md", "牢笼外的内容\n")
    assert secret.is_file()

    result = file_read(ctx, path="../../secret.md")

    assert result["ok"] is False, result
    assert result["data"]["kind"] == "outside_allowed_root"
    # 一个字节都没有读出来：拦在"打开之前"，不是打开后才发现不对
    assert "lines" not in result["data"]


def test_absolute_path_is_blocked_even_when_it_exists(ctx: ToolContext, tmp_path: Path) -> None:
    """绝对路径被拦：即使它指向一个**真实存在**的文件，也必须回 ``outside_allowed_root``。"""
    secret = _write(tmp_path / "secret.md", "牢笼外的内容\n")

    result = file_read(ctx, path=str(secret))

    assert result["ok"] is False, result
    assert result["data"]["kind"] == "outside_allowed_root"
    assert "lines" not in result["data"]


def test_symlink_pointing_outside_is_blocked(
    ctx: ToolContext, workdir: Path, tmp_path: Path
) -> None:
    """符号链接指向牢笼外被拦：判据是 ``realpath`` 之后的路径，不是入参字符串。

    ``link.md`` 这个名字在字符串上完全清白（就在会话工作目录里），只有展开链接才看得出来。
    这也是为什么不能拿 ``str.startswith`` 之类的字符串前缀做判据。
    """
    secret = _write(tmp_path / "secret.md", "牢笼外的内容\n")
    link = workdir / "link.md"
    link.symlink_to(secret)

    result = file_read(ctx, path="link.md")

    assert result["ok"] is False, result
    assert result["data"]["kind"] == "outside_allowed_root"
    assert "lines" not in result["data"]


# --------------------------------------------------------------------------- #
# 8–9. 不支持的类型 / 不存在：可读错误，不是 500
# --------------------------------------------------------------------------- #


def test_unsupported_type_returns_kind_and_supported_groups(
    ctx: ToolContext, workdir: Path
) -> None:
    """``.zip`` 回 ``ok=false`` + ``unsupported_type``，并**列出支持的组**（模型据此换办法）。

    重点是"不抛"：抛了主循环会记成工具故障，模型就只会重试同一个调用。
    """
    (workdir / "archive.zip").write_bytes(b"PK\x03\x04 not really a zip")

    result = file_read(ctx, path="archive.zip")  # 不包 pytest.raises：抛了就算失败

    assert result["ok"] is False
    data = result["data"]
    assert data["kind"] == "unsupported_type"
    assert data["extension"] == "zip"
    # 支持的组要**列给模型看**，否则它只会把同一个调用重试一遍
    assert {
        "text",
        "pdf",
        "office",
        "html",
        "ebook",
        "rtf",
    } == {item["name"] for item in data["groups"]}


def test_missing_file_returns_not_found(ctx: ToolContext) -> None:
    """不存在的文件回 ``not_found``，且**不会**顺手把它创建出来（这是只读工具）。"""
    result = file_read(ctx, path="nope.md")

    assert result["ok"] is False
    assert result["data"]["kind"] == "not_found"
    assert not (Path(ctx.extra["workdir"]) / "nope.md").exists()


# --------------------------------------------------------------------------- #
# 10. file.list：目录在前、数量与深度
# --------------------------------------------------------------------------- #


def test_list_puts_directories_first_and_respects_depth(ctx: ToolContext, workdir: Path) -> None:
    """``file.list``：目录排在文件前，``depth=1`` 只列本层（不把整棵子树倒进上下文）。"""
    _write(workdir / "b.md", "b\n")
    _write(workdir / "a" / "nested.md", "nested\n")

    shallow = file_list(ctx, path=".")
    assert shallow["ok"] is True, shallow
    data = shallow["data"]
    assert data["kind"] == "dir"
    assert data["depth"] == 1
    assert [entry["name"] for entry in data["entries"]] == ["a", "b.md"]
    assert data["entries"][0]["is_dir"] is True
    assert "a/nested.md" not in [entry["name"] for entry in data["entries"]]

    deep = file_list(ctx, path=".", depth=2)
    assert deep["ok"] is True
    assert "a/nested.md" in [entry["name"] for entry in deep["data"]["entries"]]


def test_list_is_also_caged(ctx: ToolContext, tmp_path: Path) -> None:
    """``file.list`` 与 ``file.read`` 走**同一个**牢笼（不是各写一套判定）。"""
    (tmp_path / "outside_dir").mkdir(exist_ok=True)

    result = file_list(ctx, path="../../outside_dir")

    assert result["ok"] is False
    assert result["data"]["kind"] == "outside_allowed_root"

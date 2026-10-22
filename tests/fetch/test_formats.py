"""``fetch/formats.py`` 契约：扩展名判定（**先 URL 后缀、再看 media type**）+ epub / RTF / 压缩包。

为什么单测这个：``source.fetch`` 与 ``archive.download`` 共用同一套判定，
判错后缀会让抓回来的东西以错误类型落盘 —— 把 ``.md``/``.py`` 交给 HTML 提取器洗掉缩进，
把 ``.zip`` 当正文抽（体积 + 编码 + zip bomb 三类风险）。所以口径必须钉死在用例里。

抽取器的口径与 ``office.py`` 一致：**抽不到就如实 ``ok=False`` + 原因**，不返回空串冒充成功。
"""

from __future__ import annotations

import io
import tarfile
import zipfile

import pytest

from scholarscout.fetch.formats import (
    MAX_ARCHIVE_ENTRIES,
    TS_PACKET_BYTES,
    archive_listing,
    epub_text,
    guess_extension,
    is_archive,
    legacy_office_reason,
    rtf_text,
)

# --------------------------------------------------------------- 扩展名判定


@pytest.mark.parametrize(
    ("url", "media_type", "expected"),
    [
        # URL 后缀说了算：站点常把 .md/.py 用 text/plain 甚至 octet-stream 发出来
        ("https://example.org/notes.md", "text/plain", "md"),
        ("https://example.org/notes.md", "application/octet-stream", "md"),
        ("https://example.org/tool.py", "text/plain", "py"),
        ("https://example.org/data.jsonl", "text/plain", "jsonl"),
        ("https://example.org/data.ndjson", "application/octet-stream", "ndjson"),
        # 带 query 的 URL 也要认
        ("https://example.org/notes.md?raw=1", "text/plain", "md"),
        ("https://raw.github.com/a/b/main/x.tsv#L3", "text/plain", "tsv"),
        # XML 家族（.svg 本身就是 XML 文本）
        ("https://example.org/logo.svg", "image/svg+xml", "svg"),
        ("https://example.org/feed.xml", "application/xml", "xml"),
        ("https://example.org/feed.rss", "application/rss+xml", "rss"),
        # 电子书 / RTF / 压缩包
        ("https://example.org/book.epub", "application/octet-stream", "epub"),
        ("https://example.org/doc.rtf", "application/rtf", "rtf"),
        ("https://example.org/pkg.zip", "application/zip", "archive"),
        ("https://example.org/pkg.tar", "application/x-tar", "archive"),
        ("https://example.org/pkg.tar.gz", "application/gzip", "archive"),
        ("https://example.org/pkg.tgz", "application/gzip", "archive"),
        ("https://example.org/pkg.tar.bz2", "application/x-bzip2", "archive"),
        # Office 三件套
        ("https://example.org/report.docx", "application/octet-stream", "docx"),
        ("https://example.org/sheet.xlsx", "application/octet-stream", "xlsx"),
        ("https://example.org/slides.pptx", "application/octet-stream", "pptx"),
        # 音视频：标 media（一律不抽正文，docs/19 §8.1）
        ("https://example.org/talk.mp4", "video/mp4", "media"),
        ("https://example.org/talk.mp3", "audio/mpeg", "media"),
    ],
)
def test_url_suffix_wins_over_media_type(url: str, media_type: str, expected: str) -> None:
    assert guess_extension(media_type, url) == expected


@pytest.mark.parametrize(
    ("media_type", "url", "expected"),
    [
        # URL 没有后缀时，才轮到 media type
        ("text/plain", "https://example.org/download", "txt"),
        ("application/json", "https://api.example.org/v1/items", "json"),
        ("application/xml", "https://example.org/feed", "xml"),
        ("text/html", "https://example.org/page", "html"),
        ("", "https://example.org/page", "html"),
        # Office 的正式 media type（URL 里没有后缀）
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "https://example.org/download",
            "docx",
        ),
        (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "https://example.org/download",
            "xlsx",
        ),
        ("application/epub+zip", "https://example.org/download", "epub"),
        # 认不出来的二进制：bin（不猜类型）
        ("image/png", "https://example.org/pic", "bin"),
        ("application/octet-stream", "https://example.org/blob", "bin"),
    ],
)
def test_media_type_is_the_fallback(media_type: str, url: str, expected: str) -> None:
    assert guess_extension(media_type, url) == expected


def test_pdf_flag_beats_everything_else() -> None:
    """``is_pdf=True``（fetch 层判定出来的）优先：URL 后缀与 media type 都不参与。"""
    assert guess_extension("text/html", "https://example.org/abs/2501.00001", True) == "pdf"
    assert guess_extension("", "https://example.org/x.md", True) == "pdf"


def test_unknown_suffix_falls_through_to_media_type() -> None:
    """未知后缀不当成类型用：仍按 media type 判（否则会落一个乱扩展名）。"""
    assert guess_extension("application/pdf", "https://example.org/paper.weird", True) == "pdf"


# --------------------------------------------------------------- `.ts` 双集合命中
#: 一段真实 TypeScript 源码：`.ts` 同时也是 MPEG-TS 视频的后缀，两个集合都收它，
#: 按集合先后判（MEDIA 先）会把整片源码标成"音视频不抽正文" —— 正文就被静默丢掉了。
TS_SOURCE = b"export interface Paper { id: string }\nexport const x: number = 1;\n"
#: MPEG-TS 的两个包：188 字节一包，每包首字节 0x47。
TS_VIDEO = bytes([0x47]) + b"\x00" * 187 + bytes([0x47]) + b"\x00" * 187


@pytest.mark.parametrize(
    ("media_type", "content", "expected"),
    [
        # 1) 响应头说是文本 → 源码（哪怕字节看起来像视频）
        ("text/plain", TS_SOURCE, "ts"),
        ("text/plain; charset=utf-8", TS_SOURCE, "ts"),
        ("application/json", TS_SOURCE, "ts"),
        # 2) 响应头说是视频 → 媒体
        ("video/mp2t", TS_VIDEO, "media"),
        ("video/mp2t", TS_SOURCE, "media"),
        ("audio/mpeg", TS_VIDEO, "media"),
        ("application/mp2t", TS_VIDEO, "media"),
        # 3) 没有响应头 → 看魔数；看不出是视频就归**文本**（宁可当源码，不当视频）
        ("", TS_SOURCE, "ts"),
        ("", None, "ts"),
        ("", b"", "ts"),
        ("application/octet-stream", TS_SOURCE, "ts"),
        ("application/octet-stream", TS_VIDEO, "media"),
    ],
)
def test_ts_is_decided_by_content_not_by_set_order(
    media_type: str, content: bytes | None, expected: str
) -> None:
    assert guess_extension(media_type, "https://example.org/worker.ts", content=content) == expected


def test_ts_without_content_argument_degrades_to_text() -> None:
    """拿不到字节（老调用方只传两个参数）时归文本 —— 不许回退成"视频"。"""
    assert guess_extension("", "https://example.org/worker.ts") == "ts"
    assert guess_extension("application/octet-stream", "https://example.org/worker.ts") == "ts"


def test_octet_stream_with_real_ts_magic_is_media() -> None:
    """最不该出错的一条：站点把视频当 `application/octet-stream` 发（CDN 默认行为）。

    响应头说不清、字节是真 MPEG-TS（188 字节一包、两个包首字节都是 0x47）→ 必须判 media；
    若这里判成 `ts`，一段视频会被当源码塞进正文抽取，落库一堆二进制垃圾。
    """
    two_packets = bytes([0x47]) + b"\x00" * 187 + bytes([0x47]) + b"\x00" * 187
    assert len(two_packets) == TS_PACKET_BYTES * 2
    assert (
        guess_extension(
            "application/octet-stream", "https://cdn.example.com/clip.ts", content=two_packets
        )
        == "media"
    )


def test_mpeg_ts_magic_needs_two_sync_packets() -> None:
    """魔数判定要"连看两包"：只有一个 0x47 的短内容不算视频（否则一段文本也能凑出来）。"""
    assert (
        guess_extension("", "https://example.org/a.ts", content=bytes([0x47]) + b"x" * 20) == "ts"
    )
    assert (
        guess_extension("", "https://example.org/a.ts", content=bytes([0x47]) * 188 + b"\x00" * 188)
        == "ts"
    )


def test_doc_suffix_is_recognized_as_legacy_office() -> None:
    """`.doc` 不许折成 docx、更不许落到 html/bin —— 它是 OLE 二进制，读不了要如实说。

    落进 html 提取器是最糟的一条：二进制被洗成一段乱码正文，还标 `ok`（用户以为抓到了）。
    """
    assert guess_extension("application/msword", "https://example.org/report.doc") == "doc"
    # URL 看不出后缀时也得认出 media type
    assert guess_extension("application/msword", "https://example.org/download") == "doc"
    assert guess_extension("application/x-msword", "https://example.org/download") == "doc"
    assert guess_extension("application/octet-stream", "https://example.org/report.doc") == "doc"
    assert guess_extension("", "https://example.org/report.doc") == "doc"
    # 原因得说清该怎么办
    assert "需先转换为 .docx/.xlsx/.pptx" in legacy_office_reason("doc")


def test_xls_suffix_is_not_folded_into_xlsx() -> None:
    """`.xls` 原来是"假支持"：被改写成 xlsx 去解 zip，报"不是有效的 .xlsx"（误导）。

    真因是它是老二进制格式，跟 xlsx 是两种东西 —— 判定必须留在 `xls`，让调用方给转换提示。
    """
    assert guess_extension("application/vnd.ms-excel", "https://example.org/grade.xls") == "xls"
    assert guess_extension("application/vnd.ms-excel", "https://example.org/download") == "xls"
    assert guess_extension("application/octet-stream", "https://example.org/grade.xls") == "xls"
    # 而真正的 OOXML 仍然认成 xlsx
    assert guess_extension("", "https://example.org/grade.xlsx") == "xlsx"
    assert "需先转换为" in legacy_office_reason("xls")


def test_ppt_suffix_is_recognized_as_legacy_office() -> None:
    """`.ppt` 靠 media type 里的 `presentation` 混进了 pptx 分支 —— 同样读不了。"""
    assert guess_extension("application/vnd.ms-powerpoint", "https://example.org/deck.ppt") == "ppt"
    assert guess_extension("application/vnd.ms-powerpoint", "https://example.org/download") == "ppt"
    assert guess_extension("application/octet-stream", "https://example.org/deck.ppt") == "ppt"
    assert guess_extension("", "https://example.org/deck.pptx") == "pptx"
    assert "需先转换为" in legacy_office_reason("ppt")


def test_is_archive_matches_only_archives() -> None:
    assert is_archive("zip") and is_archive("tar") and is_archive("tgz")
    assert not is_archive("md") and not is_archive("epub") and not is_archive("")


# --------------------------------------------------------------- epub


def _zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def test_epub_text_extracts_chapters_in_name_order() -> None:
    content = _zip_bytes(
        {
            "OEBPS/chapter2.xhtml": "<html><body><p>第二章：判权规则</p></body></html>",
            "OEBPS/chapter1.xhtml": "<html><body><p>第一章：检索</p></body></html>",
            "mimetype": "application/epub+zip",
        }
    )

    ok, text, detail = epub_text(content)

    assert ok is True
    assert "第一章：检索" in text and "第二章：判权规则" in text
    assert text.index("第一章") < text.index("第二章")  # 按文件名排序是确定性的
    assert "# chapter1.xhtml" in text
    assert detail == "epub：2 章"


def test_epub_text_without_chapters_reports_why() -> None:
    ok, text, detail = epub_text(_zip_bytes({"mimetype": "application/epub+zip"}))

    assert ok is False and text == ""
    assert "没有 XHTML/HTML 章节" in detail


def test_epub_text_rejects_non_zip() -> None:
    ok, text, detail = epub_text("这不是 epub".encode())

    assert ok is False and text == ""
    assert "不是有效的 .epub" in detail


# --------------------------------------------------------------- RTF


def test_rtf_text_strips_control_words_and_hex_escapes() -> None:
    content = rb"{\rtf1\ansi\deff0\fs24 Hello \'e9\par World\tab End}"

    ok, text, detail = rtf_text(content)

    assert ok is True
    assert text.startswith("Hello é")  # \'e9 → cp1252 的 é
    assert "World" in text and "\t" in text
    assert "rtf1" not in text and "\\" not in text and "{" not in text
    assert detail.startswith("rtf：")


def test_rtf_text_handles_empty_body() -> None:
    ok, text, _detail = rtf_text(rb"{\rtf1\ansi}")

    assert ok is True and text == ""


# --------------------------------------------------------------- 压缩包目录


def test_archive_listing_lists_zip_entries_only() -> None:
    content = _zip_bytes({"src/a.py": "print(1)", "README.md": "# hi"})

    ok, text, detail = archive_listing(content, "archive")

    assert ok is True
    assert "src/a.py" in text and "README.md" in text
    assert text.startswith("# 压缩包目录（2 项，只登记不抽取内容）（archive）")
    assert detail == "archive：2 项"


def test_archive_listing_lists_tar_entries() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        payload = b"print(1)"
        info = tarfile.TarInfo("pkg/main.py")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    content = buffer.getvalue()

    ok, text, detail = archive_listing(content, "tar")

    assert ok is True
    assert "pkg/main.py" in text
    assert detail == "archive：1 项"


def test_archive_listing_caps_the_listing() -> None:
    """目录清单有上限：一个几十万条的表会把库撑爆，超出部分只报数量。"""
    files = {f"file{i}.txt": "x" for i in range(MAX_ARCHIVE_ENTRIES + 3)}

    ok, text, detail = archive_listing(_zip_bytes(files), "zip")

    assert ok is True
    assert "还有 3 项未列出" in text
    assert detail == f"archive：{MAX_ARCHIVE_ENTRIES + 3} 项"


def test_archive_listing_rejects_garbage() -> None:
    ok, text, detail = archive_listing(b"not an archive at all", "zip")

    assert ok is False and text == ""
    assert "不是有效的压缩包" in detail

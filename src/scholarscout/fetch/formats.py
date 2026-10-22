"""抓取格式判定与"额外格式"抽取器 —— 只用标准库，零新增运行期依赖。

放这里的理由：扩展名判定被 **工具层（`tools/source_fetch.py`）与协议层（`rpc/archive.py` 的按需下载）
共用**，放在 `fetch/`（最底层、只依赖标准库）两边都能 import，不会打穿分层
（`tests/test_layering.py`：`rpc` 可以依赖 `fetch`，`fetch` 不许反过来）。

三件事：
1. `guess_extension` —— **先看 URL 后缀再看 media type**（很多站点把 `.md`/`.py` 用
   `text/plain` 甚至 `application/octet-stream` 发出来，只看 media type 会一律认成 txt）。
   **例外**：`.ts` 既是 TypeScript 源码又是 MPEG-TS 视频，双集合命中 → 按内容判
   （`resolve_ambiguous_ts`：Content-Type → 魔数 → 拿不准归文本）。
2. `epub_text` / `rtf_text` —— 标准库抽字（zip + XHTML；RTF 控制字剥离）。
3. `archive_listing` —— `.zip/.tar*` **只列目录当元数据**（不解内容）。

口径与 `office.py` 一致：抽不到就如实返回 `ok=False` + 原因，**不返回空串冒充"提取成功"**。
"""

from __future__ import annotations

import io
import re
import tarfile
import zipfile

#: 音视频：用户明确要求**不**抓正文（只保留资料链接与大小）。
MEDIA_SUFFIXES = {
    "mp3",
    "wav",
    "flac",
    "aac",
    "ogg",
    "m4a",
    "wma",
    "opus",
    "mp4",
    "avi",
    "mkv",
    "mov",
    "wmv",
    "flv",
    "webm",
    "m4v",
    "mpg",
    "mpeg",
    "ts",
}

#: Office 三件套：走 `fetch/office.py` 的标准库抽取器（zip + XML，不引依赖）。
OFFICE_SUFFIXES = {"docx", "xlsx", "pptx", "xls"}

#: 电子书：走本模块的 `epub_text`（zip + XHTML）。
EBOOK_SUFFIXES = {"epub"}

#: RTF：走本模块的 `rtf_text`（控制字剥离）。
RTF_SUFFIXES = {"rtf"}

#: 压缩包：**不解内容、只列目录**（源码包/数据集打包常见）。
ARCHIVE_SUFFIXES = {"zip", "tar", "tgz", "gz", "bz2", "xz"}

#: 文本类后缀：**原样解码**，不要拿 HTML 提取器去洗（洗了就没了缩进和代码结构）。
TEXT_SUFFIXES = {
    "md",
    "markdown",
    "txt",
    "py",
    "js",
    "ts",
    "jsx",
    "tsx",
    "json",
    "jsonl",
    "ndjson",
    "yaml",
    "yml",
    "toml",
    "ini",
    "cfg",
    "csv",
    "tsv",
    "ipynb",
    "c",
    "cc",
    "cpp",
    "h",
    "hpp",
    "java",
    "go",
    "rs",
    "rb",
    "sh",
    "bat",
    "ps1",
    "sql",
    "tex",
    "r",
    "jl",
    "m",
    "patch",
    "diff",
    "log",
    # XML 家族：本质是文本，原样解码即可（`.svg` 就是 XML 文本）
    "xml",
    "svg",
    "xsl",
    "xsd",
    "rss",
    "atom",
    "graphql",
    "vue",
}

#: 压缩包目录最多列多少项（防一个几十万条的清单把库撑爆）。
MAX_ARCHIVE_ENTRIES = 500

#: **双集合命中**的后缀：`.ts` 同时是 TypeScript 源码与 MPEG-TS 视频。两个集合都收它，
#: 谁先判谁就赢 —— 原来 MEDIA 先判，于是整片 TypeScript 源码被标成"音视频不抽正文"。
#: 这一组必须**按内容判**（见 `resolve_ambiguous_ts`）。
AMBIGUOUS_SUFFIXES = frozenset({"ts"})

#: MPEG-TS：188 字节一包，每包首字节是同步字节 0x47（连看两包就够确定）。
TS_PACKET_BYTES = 188
TS_SYNC_BYTE = 0x47


def _looks_like_mpeg_ts(content: bytes | None) -> bool:
    """魔数判定：前两个 TS 包的同步字节都在（内容太短就不算）。"""
    if not content or len(content) < TS_PACKET_BYTES * 2:
        return False
    return content[0] == TS_SYNC_BYTE and content[TS_PACKET_BYTES] == TS_SYNC_BYTE


def _is_text_media_type(media_type: str) -> bool:
    """`text/*` 与常见的"其实是文本"的 application 类型。"""
    return media_type.startswith("text/") or any(
        marker in media_type for marker in ("json", "javascript", "xml", "yaml")
    )


def resolve_ambiguous_ts(media_type: str, content: bytes | None = None) -> str:
    """`.ts` 的判定表（**按内容判，不按集合顺序判**）：

    | 响应头 | 内容 | 结果 |
    |---|---|---|
    | `text/*`、`*json*`、`*javascript*`、`*xml*` | — | `ts`（源码） |
    | `video/*`、`audio/*`、`application/mp2t` | — | `media` |
    | 缺失 / `application/octet-stream` 等说不清的 | 前两包都是 0x47 | `media` |
    | 缺失 / 说不清的 | 其它（含读不到字节） | `ts`（源码） |

    **拿不准归文本**：把一段视频当文本，代价只是抽不出正文（会标 `empty`）；
    把一份 TypeScript 源码当视频，代价是**静默丢掉正文**（标成"音视频不抽正文"）。
    后者更糟，所以默认落在文本这一边。
    """
    lowered = (media_type or "").strip().lower()
    if lowered.startswith(("video/", "audio/")) or lowered == "application/mp2t":
        return "media"
    if _is_text_media_type(lowered):
        return "ts"
    return "media" if _looks_like_mpeg_ts(content) else "ts"


def _url_name(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1]


def guess_extension(
    media_type: str,
    url: str,
    is_pdf: bool = False,
    content: bytes | None = None,
) -> str:
    """落盘与展示用的类型标签（先把 `source.fetch` 里的私有 `_extension` 提出来共用）。

    判定顺序：`is_pdf` → URL 后缀（**双集合命中的 `.ts` 先按内容判** → media →
    office/ebook/rtf/archive/text）→ media type。

    ``content`` 只在需要魔数判定时才用（目前只有 `.ts`）。拿不到字节时按"文本"处理 ——
    见 `resolve_ambiguous_ts` 里的取舍说明（宁可当源码，不当视频）。
    """
    if is_pdf:
        return "pdf"
    lowered = (media_type or "").lower()
    name = _url_name(url)
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix in AMBIGUOUS_SUFFIXES:
        # `.ts` 同时在 MEDIA 与 TEXT 两个集合里：按集合先后判会把 TypeScript 当视频
        return resolve_ambiguous_ts(lowered, content)
    if suffix in MEDIA_SUFFIXES:
        return "media"
    if suffix in OFFICE_SUFFIXES or "officedocument" in lowered or "msword" in lowered:
        return (
            "docx"
            if (suffix == "docx" or "word" in lowered)
            else (
                "xlsx"
                if (suffix in {"xlsx", "xls"} or "excel" in lowered or "spreadsheet" in lowered)
                else ("pptx" if (suffix == "pptx" or "presentation" in lowered) else suffix)
            )
        )
    if suffix in EBOOK_SUFFIXES or "epub" in lowered:
        return "epub"
    if suffix in RTF_SUFFIXES or "rtf" in lowered:
        return "rtf"
    if suffix in ARCHIVE_SUFFIXES:
        return "archive"
    if suffix in TEXT_SUFFIXES:
        return suffix
    if "html" in lowered or not lowered:
        return "html"
    if "json" in lowered:
        return "json"
    if "xml" in lowered or "atom" in lowered:
        return "xml"
    if "text/plain" in lowered:
        return "txt"
    return "bin"


# ------------------------------------------------------------------ epub


def epub_text(content: bytes) -> tuple[bool, str, str]:
    """`.epub` 抽正文：zip 里的 XHTML/HTML 章节按名排序，逐个抽字后拼接。

    不做 EPUB 的 spine 排序（要解析 OPF，收益低）—— 按文件名排序是**确定性**的，
    且真实 epub 的章节文件名多为 `chapter1.xhtml`、`part_01.xhtml` 这类天然有序的形态。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = [
                item
                for item in archive.namelist()
                if item.lower().endswith((".xhtml", ".html", ".htm"))
            ]
            names.sort()
            if not names:
                return False, "", "epub 里没有 XHTML/HTML 章节"
            parts: list[str] = []
            for name in names:
                raw = archive.read(name)
                from scholarscout.fetch.html_text import extract_html

                body = extract_html(raw.decode("utf-8", errors="replace")).text
                if body.strip():
                    parts.append(f"# {name.rsplit('/', 1)[-1]}\n{body.strip()}")
    except (zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
        return False, "", f"不是有效的 .epub（{type(exc).__name__}）"
    text = "\n\n".join(parts).strip()
    return True, text, f"epub：{len(names)} 章"


# ------------------------------------------------------------------ rtf

_RTF_HEX_RE = re.compile(r"\\'([0-9a-fA-F]{2})")
_RTF_CONTROL_RE = re.compile(r"\\[a-zA-Z]+-?\d*\s?")
#: RTF 头里这些组是字体表/颜色表/样式表/元信息，抽正文时整段丢掉。
_RTF_META_RE = re.compile(
    r"\{\\\*?\\?(?:fonttbl|colortbl|stylesheet|info|pict|header|footer)[^{}]*\}", re.S
)


def rtf_text(content: bytes) -> tuple[bool, str, str]:
    """`.rtf` 抽正文：把 RTF 控制字剥离成纯文本（只做"够用"的一版，不做完整 RTF 解析）。

    `\\'hh`（十六进制字符）按 cp1252 还原；`\\par`/`\\line` → 换行；`\\tab` → 制表符；
    其余控制字（`\\fs24` 等）与分组花括号丢掉；转义 `\\{ \\} \\\\` 还原。
    """
    try:
        raw = content.decode("latin-1")
    except UnicodeDecodeError as exc:  # pragma: no cover - latin-1 几乎不会失败
        return False, "", f"rtf 解码失败：{type(exc).__name__}"
    text = _RTF_META_RE.sub(" ", raw)
    text = _RTF_HEX_RE.sub(
        lambda match: bytes([int(match.group(1), 16)]).decode("cp1252", errors="replace"), text
    )
    text = text.replace("\\par", "\n").replace("\\line", "\n").replace("\\tab", "\t")
    text = _RTF_CONTROL_RE.sub("", text)
    text = text.replace("\\{", "{").replace("\\}", "}").replace("\\\\", "\\")
    text = re.sub(r"[{}]", "", text)
    lines = [line.strip() for line in text.splitlines()]
    out = "\n".join(line for line in lines if line).strip()
    return True, out, f"rtf：{len(out)} 字符"


# ------------------------------------------------------------------ 压缩包


def archive_listing(content: bytes, ext: str = "") -> tuple[bool, str, str]:
    """`.zip/.tar*` **只列目录**（名字 + 大小），不解内容。

    为什么只列目录：用户明确要的是"尽量多抓格式"，但**把源码包/数据集解包抽正文**会带来
    体积、编码、炸弹（zip bomb）三类风险，而且解出来的成千上万个文件也无处安放。
    作为**元数据**列出目录足够让模型知道"这个包里大概有什么"。
    """
    entries: list[str] = []
    try:
        if zipfile.is_zipfile(io.BytesIO(content)):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                for info in archive.infolist():
                    entries.append(f"{info.filename}\t{info.file_size}")
        else:
            with tarfile.open(fileobj=io.BytesIO(content)) as tar:
                for member in tar.getmembers():
                    entries.append(f"{member.name}\t{member.size}")
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError, ValueError) as exc:
        return False, "", f"不是有效的压缩包（{type(exc).__name__}）"
    total = len(entries)
    shown = entries[:MAX_ARCHIVE_ENTRIES]
    header = f"# 压缩包目录（{total} 项，只登记不抽取内容）（{ext or 'archive'}）"
    if total > MAX_ARCHIVE_ENTRIES:
        shown.append(f"…（还有 {total - MAX_ARCHIVE_ENTRIES} 项未列出）")
    return True, "\n".join([header, *shown]), f"archive：{total} 项"


#: 供外部判断"要不要走压缩包/电子书/rtf 分支"
def is_archive(extension: str) -> bool:
    return extension in ARCHIVE_SUFFIXES


__all__ = [
    "AMBIGUOUS_SUFFIXES",
    "ARCHIVE_SUFFIXES",
    "EBOOK_SUFFIXES",
    "MAX_ARCHIVE_ENTRIES",
    "MEDIA_SUFFIXES",
    "OFFICE_SUFFIXES",
    "RTF_SUFFIXES",
    "TEXT_SUFFIXES",
    "TS_PACKET_BYTES",
    "TS_SYNC_BYTE",
    "archive_listing",
    "epub_text",
    "guess_extension",
    "is_archive",
    "resolve_ambiguous_ts",
    "rtf_text",
]

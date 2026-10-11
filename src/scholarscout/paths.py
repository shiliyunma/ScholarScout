"""所有落盘路径的唯一入口。

规则 §3.8：落盘路径必须经本模块拼接并断言在 ``data/`` 之下，防 ``../`` 逃逸。
其他模块**不许**自己拼 ``data/...`` 字符串。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

#: data/ 下允许出现的子目录（超出这个集合即视为越界）
DATA_SUBDIRS: tuple[str, ...] = ("raw", "text", "notes", "cache", "library")

#: 文件名 slug 的长度上限（按字符计，中英一视同仁）。超出截断并用 ``~`` 收尾（docs/19 §1）。
SLUG_MAX = 60

#: Windows 文件名非法字符：``< > : " / \ | ? *``（冒号在标题里很常见）。
_WINDOWS_ILLEGAL = frozenset('<>:"/\\|?*')

#: 控制字符（0x00–0x1f 与 DEL）：文件名里出现它们等于给自己埋坑。
_CONTROL_MAX = 32
_CONTROL_DEL = 127

#: 来源类型 → 人类可读的内容类型目录名（docs/19 §2）。认不出来的落 ``other``（不编类型）。
DOC_TYPE_BY_SOURCE: dict[str, str] = {
    "arxiv": "paper",
    "openalex": "paper",
    "semantic_scholar": "paper",
    "doi": "paper",
    "github": "code",
    "official": "doc",
    "web": "doc",
    "video": "video",
}

#: ``<年-月>`` 桶的合法形态：``2024-10``（月份必须 01–12）。不合规一律落 ``unknown``。
_YEAR_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

#: 年份未知时的桶名（与 docs/05「未知年份不编」同口径）。
UNKNOWN_BUCKET = "unknown"

_ENV_HOME = "SCHOLARSCOUT_HOME"


class PathOutsideDataError(ValueError):
    """请求的路径落在 data/ 之外。"""


def home() -> Path:
    """项目根目录：环境变量优先；打包后取 exe 所在目录；否则取包目录的上三级。

    为什么打包后要换：PyInstaller 把代码放进临时解包目录（onefile）或 `_internal/`（onedir），
    **那个位置不能放数据** —— `data/`、`run/state.db`、`config.yaml` 必须落在用户看得见、
    拷得走的地方。所以冻结模式下统一用 `sys.executable` 的目录：exe 在哪，数据就在哪（便携）。
    """
    env = os.environ.get(_ENV_HOME)
    if env:
        return Path(env).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def data_dir(root: Path | None = None) -> Path:
    return (root or home()) / "data"


def run_dir(root: Path | None = None) -> Path:
    return (root or home()) / "run"


def db_path(root: Path | None = None) -> Path:
    return run_dir(root) / "state.db"


def ensure_dirs(root: Path | None = None) -> None:
    """建出 data/ 的子目录与 run/。幂等。"""
    base = root or home()
    for name in DATA_SUBDIRS:
        (data_dir(base) / name).mkdir(parents=True, exist_ok=True)
    run_dir(base).mkdir(parents=True, exist_ok=True)


def under_data(rel: str | Path, root: Path | None = None) -> Path:
    """把相对路径解析到 data/ 之下；越界直接抛（调用方不许 try 掉它）。"""
    base = data_dir(root).resolve()
    candidate = (base / rel).resolve()
    if candidate != base and base not in candidate.parents:
        raise PathOutsideDataError(f"路径越出 data/：{rel!r} -> {candidate}")
    return candidate


def raw_path_for(sha256: str, ext: str, root: Path | None = None) -> Path:
    """原件路径：``data/raw/<sha前2位>/<sha>.<ext>``。"""
    if len(sha256) < 2 or not sha256.isalnum():
        raise ValueError("sha256 必须是十六进制字符串")
    suffix = ext if ext.startswith(".") else f".{ext}"
    return under_data(Path("raw") / sha256[:2] / f"{sha256}{suffix}", root)


def text_path_for(doc_id: str, root: Path | None = None) -> Path:
    """抽取出的全文纯文本：``data/text/<doc_id>.txt``。

    **旧口径，仅作回落**（历史数据/老测试）。生产路径一律用 :func:`text_path_for_doc`，
    全文路径不再由 ``doc_id`` 拼 —— 改名后 ``doc_id`` 与文件名再无关系（docs/19 §4）。
    """
    if not doc_id or "/" in doc_id or "\\" in doc_id or ".." in doc_id:
        raise ValueError(f"非法 doc_id：{doc_id!r}")
    return under_data(Path("text") / f"{doc_id}.txt", root)


def doc_type_for(source_type: str | None) -> str:
    """来源类型 → 内容类型目录名（``paper`` / ``doc`` / ``code`` / ``video`` …）。

    认不出来一律 ``other``：**不编类型**。来源类型取自 ``sources.source_type``（docs/19 §2）。
    """
    return DOC_TYPE_BY_SOURCE.get((source_type or "").strip().lower(), "other")


def year_month_bucket(year_month: str | None) -> str:
    """把 ``2024-10`` 校验成桶名；取不到月份一律落 ``unknown``（绝不用抓取日期冒充）。

    与 docs/05「未知年份不编」同口径：宁可单列一桶，也不猜一个看着像真的月份。
    """
    value = (year_month or "").strip()
    return value if _YEAR_MONTH_RE.match(value) else UNKNOWN_BUCKET


def slugify(title: str, *, fallback: str = "", max_len: int = SLUG_MAX) -> str:
    """把任意标题转成可读、跨平台安全的文件名主体（docs/19 §1）。

    规则：去掉 Windows 非法字符与控制字符；空白与下划线 → ``-``；连续 ``-`` 合并、首尾 ``-`` 去掉；
    **保留中英文**（不做拼音化，可读性优先）；超过 ``max_len`` 截断并在末尾补 ``~``；
    空则回落到 ``fallback``（通常传域名）；都没有就给 ``untitled``。
    """
    out: list[str] = []
    for ch in title or "":
        code = ord(ch)
        if code < _CONTROL_MAX or code == _CONTROL_DEL:
            continue
        if ch in _WINDOWS_ILLEGAL or ch.isspace() or ch == "_":
            out.append("-")
        else:
            out.append(ch)
    slug = _tidy_slug("".join(out), max_len)
    if not slug:
        slug = _tidy_slug(fallback or "", max_len)
    return slug or "untitled"


def _tidy_slug(text: str, max_len: int) -> str:
    """合并连续 ``-``、去掉首尾 ``-`` 与 Windows 不允许的尾随点/空格，再按上限截断。"""
    slug = re.sub(r"-{2,}", "-", text).strip("-").strip(". ")
    if len(slug) > max_len:
        # 截到 max_len-1 再补 ``~``，保证**含 ``~`` 在内**不超过上限（docs/19 §1）
        slug = slug[: max_len - 1].rstrip("-").rstrip(". ") + "~"
    return slug


def doc_basename(title: str, sha256: str, *, domain: str = "") -> str:
    """``<标题 slug>--<sha8>``；标题取不到就回落成 ``<域名 slug>--<sha8>``（绝不编标题）。"""
    if len(sha256) < 8 or not sha256.isalnum():
        raise ValueError("sha256 必须是十六进制字符串")
    return f"{slugify(title, fallback=domain)}--{sha256[:8]}"


def raw_path_for_doc(
    source_type: str | None,
    year_month: str | None,
    title: str,
    sha256: str,
    ext: str,
    *,
    domain: str = "",
    root: Path | None = None,
) -> Path:
    """原件路径：``data/raw/<类型>/<年-月>/<标题>--<sha8>.<ext>``（docs/19 §2）。"""
    bucket = doc_type_for(source_type)
    month = year_month_bucket(year_month)
    name = doc_basename(title, sha256, domain=domain)
    return under_data(Path("raw") / bucket / month / f"{name}{_suffix(ext)}", root)


def text_path_for_doc(
    source_type: str | None,
    year_month: str | None,
    title: str,
    sha256: str,
    *,
    domain: str = "",
    root: Path | None = None,
) -> Path:
    """全文纯文本路径：**与原件同桶同名，只差后缀**（``.txt``）—— 打开目录一眼能对上。"""
    bucket = doc_type_for(source_type)
    month = year_month_bucket(year_month)
    name = doc_basename(title, sha256, domain=domain)
    return under_data(Path("text") / bucket / month / f"{name}.txt", root)


def _suffix(ext: str) -> str:
    return ext if ext.startswith(".") else f".{ext}"


def note_path_for(note_id: str, root: Path | None = None) -> Path:
    """导出的笔记：``data/notes/<note_id>.md``。"""
    if not note_id or "/" in note_id or "\\" in note_id or ".." in note_id:
        raise ValueError(f"非法 note_id：{note_id!r}")
    return under_data(Path("notes") / f"{note_id}.md", root)


def rel_to_home(path: Path, root: Path | None = None) -> str:
    """转成相对项目根的 POSIX 形式，用于写进数据库（库 里只存相对路径）。"""
    base = (root or home()).resolve()
    return path.resolve().relative_to(base).as_posix()


def resolve_rel(rel: str | Path, root: Path | None = None) -> Path:
    """把库里存的相对路径（``data/raw/ab/xx.pdf``）解析回绝对路径。

    只允许落在 ``data/`` 或 ``run/`` 之下 —— 前端的"打开原件/定位文件"走这里，
    否则一个被篡改的库就能让桌面壳打开任意文件。
    """
    base = (root or home()).resolve()
    candidate = (base / rel).resolve()
    allowed = (data_dir(root).resolve(), run_dir(root).resolve())
    if not any(candidate == prefix or prefix in candidate.parents for prefix in allowed):
        raise PathOutsideDataError(f"路径越出 data/ 与 run/：{rel!r} -> {candidate}")
    return candidate

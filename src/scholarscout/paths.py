"""所有落盘路径的唯一入口。

规则 §3.8：落盘路径必须经本模块拼接并断言在 ``data/`` 之下，防 ``../`` 逃逸。
其他模块**不许**自己拼 ``data/...`` 字符串。
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable
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

#: 会话目录名未知时用的段值（没有会话行 / 没有 session_id 时）。
UNKNOWN_SESSION = "unknown"

#: 布局段的合法取值（docs/19 §2）：``collected``=收录年月、``published``=发表年月、
#: ``session``=会话（目录名，含同名时的短 id）、``format``=格式（``type`` 是它的旧称）。
LAYOUT_SEGMENTS: tuple[str, ...] = ("collected", "published", "session", "format", "type")

#: **默认布局**（docs/19 §2）：``<会话>/<格式>`` —— 按会话分化、会话内按格式分类。
#: 这一处就是「目录如何切分」的默认值；``paths``、``tools`` 与迁移脚本都从布局参数读，
#: 不再把布局写死在三处（用户以后要再改，只改配置里的 ``library.layout``）。
DEFAULT_LAYOUT: tuple[str, ...] = ("session", "format")

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


def skills_dir(root: Path | None = None) -> Path:
    """本地技能库目录 ``<项目根>/skills``（``SKILL.md`` 的根）。

    **不在 ``data/`` 之下**，所以不走 :func:`under_data`：它是随仓库分发的**只读**素材，
    不是用户数据。索引层的唯一入口是 ``skills/index.get_index()``，别处不要自己拼。
    """
    return (root or home()) / "skills"


def db_path(root: Path | None = None) -> Path:
    return run_dir(root) / "state.db"


def log_dir(root: Path | None = None) -> Path:
    """进程日志目录 ``run/logs/``（主进程/桌面进程写这里；渠道子进程另用 ``run/channels/``）。"""
    return run_dir(root) / "logs"


def app_log_path(root: Path | None = None) -> Path:
    """主进程日志文件 ``run/logs/app.log``（轮转参数见 ``logging_setup``）。"""
    return log_dir(root) / "app.log"


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


def kb_index_path(root: Path | None = None) -> Path:
    """知识库检索索引：``data/cache/kb-fts.sqlite3``（`kb.search` 的底座，docs/01 §5.8）。

    **为什么单独一个库、还放在 ``cache/`` 里**：索引里必然出现全文（FTS5 要存倒排与原文，
    否则做不出 `snippet()`），而项目的硬约束是**正文不入库**（`documents` 只存 `text_path`）。
    把它拆成独立文件之后：① 资料库本体 `run/state.db` 里一个正文字节都没有，可以逐字节验证；
    ② 索引是**可丢弃缓存** —— 删掉照样能跑，下次检索按 `data/text/` 重建。
    """
    return under_data(Path("cache") / "kb-fts.sqlite3", root)


def text_path_for(doc_id: str, root: Path | None = None) -> Path:
    """抽取出的全文纯文本：``data/text/<doc_id>.txt``。

    **最老的一版口径，仅作历史回落**（库里早期数据可能记着这种路径）。生产路径一律走
    :func:`text_path_for_layout` —— 全文路径不再由 ``doc_id`` 拼，改名后 ``doc_id`` 与文件名
    再无关系（docs/19 §2/§4）。
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


def _suffix(ext: str) -> str:
    return ext if ext.startswith(".") else f".{ext}"


def normalize_layout(layout: str | Iterable[str] | None) -> tuple[str, ...]:
    """把布局参数规整成段名元组；**未知段直接报错**（宁可当场炸，也不静默写到错位置）。

    接受 ``"session/format"`` 这样的字符串或 ``["session", "format"]`` 这样的序列；
    ``None`` 用 :data:`DEFAULT_LAYOUT`。合法段见 :data:`LAYOUT_SEGMENTS`。
    """
    if layout is None:
        return DEFAULT_LAYOUT
    source = layout.split("/") if isinstance(layout, str) else layout
    parts = tuple(str(part).strip() for part in source)
    parts = tuple(part for part in parts if part)
    if not parts:
        raise ValueError("布局参数不能为空；默认是 " + "/".join(DEFAULT_LAYOUT))
    unknown = [part for part in parts if part not in LAYOUT_SEGMENTS]
    if unknown:
        raise ValueError(f"未知的布局段：{unknown}；可用段：{list(LAYOUT_SEGMENTS)}")
    return parts


def _one_component(value: str) -> str:
    """目录名只能是一段路径：抹掉 ``..``、把分隔符换成 ``-``（规则 §3.8：不许越出 data/）。

    抹掉 ``..`` **先于**换分隔符：否则 ``../..`` 会被换成 ``..-..`` 再变成 ``---``，看着不像逃逸，
    却也没落回 ``unknown``（``---`` 是个能骗过眼、又没意义的目录名）。
    """
    text = str(value or "").replace("..", "").replace("\\", "-").replace("/", "-")
    return text.strip("-").strip()


def layout_dir(
    layout: str | Iterable[str] | None = None,
    *,
    session: str = "",
    fmt: str = "",
    collected: str = "",
    published: str = "",
) -> Path:
    """按布局把资料落到哪一层目录拼出来（相对 ``data/raw`` / ``data/text``）。

    - ``session``：**已解析好的会话目录名**（由 ``library.naming.session_dir_name`` 出，含同名短 id）；
      缺了落 :data:`UNKNOWN_SESSION`。
    - ``fmt``：格式目录名（``paths.doc_type_for(sources.source_type)`` 的结果），缺了落 ``other``。
    - ``collected`` / ``published``：收录年月 / 发表年月（``YYYY-MM``）；不合规落 ``unknown``。
    """
    values = {
        "collected": year_month_bucket(collected),
        "published": year_month_bucket(published),
        "session": _one_component(session) or UNKNOWN_SESSION,
        "format": _one_component(fmt) or "other",
    }
    values["type"] = values["format"]
    return Path(*[values[segment] for segment in normalize_layout(layout)])


def raw_path_for_layout(
    layout: str | Iterable[str] | None = None,
    *,
    session: str = "",
    fmt: str = "",
    collected: str = "",
    published: str = "",
    title: str = "",
    sha256: str = "",
    ext: str = "",
    domain: str = "",
    root: Path | None = None,
) -> Path:
    """按布局拼原件路径：``data/raw/<布局段…>/<标题>--<sha8>.<ext>``（docs/19 §2）。"""
    name = doc_basename(title, sha256, domain=domain)
    target = (
        Path("raw")
        / layout_dir(layout, session=session, fmt=fmt, collected=collected, published=published)
        / f"{name}{_suffix(ext)}"
    )
    return under_data(target, root)


def text_path_for_layout(
    layout: str | Iterable[str] | None = None,
    *,
    session: str = "",
    fmt: str = "",
    collected: str = "",
    published: str = "",
    title: str = "",
    sha256: str = "",
    domain: str = "",
    root: Path | None = None,
) -> Path:
    """按布局拼全文路径：**与原件同目录同名，只差后缀**（打开目录一眼能对上）。"""
    name = doc_basename(title, sha256, domain=domain)
    target = (
        Path("text")
        / layout_dir(layout, session=session, fmt=fmt, collected=collected, published=published)
        / f"{name}.txt"
    )
    return under_data(target, root)


def library_index_path(bucket: str | Path, root: Path | None = None) -> Path:
    """某个桶的清单：``data/library/<桶目录>/index.md``（``bucket`` 为空则指全库清单）。"""
    relative = Path("library") / (Path(bucket) if str(bucket) else Path()) / "index.md"
    return under_data(relative, root)


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

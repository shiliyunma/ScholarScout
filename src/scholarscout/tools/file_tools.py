"""``file.list`` / ``file.read`` —— **只读**文件工具（docs/01 §3.16）。

**为什么要它们**：读一份本地文件不该走 `py.exec`。`py.exec` 的护栏是"跑之前问用户"，每读一次
弹一次审批会造成审批疲劳 —— 用户点麻了就会连危险操作一起点过，整个审批设计就废了。
所以读文件单独开**只读工具**：没有写入参数、不创建文件、不存在就不存在，
真正需要动笔（改 docx、跑脚本）才回 `py.exec` 走审批。

**复用而不是另写一套**：按扩展名路由到**已有的抽取器**（`fetch/office.py` 的
``office_text``、`fetch/html_text.py` 的 ``extract_html``、`fetch/pdf_text.py` 的 ``pdf_text``、
`fetch/formats.py` 的 ``epub_text`` / ``rtf_text``）。后缀集合也直接用 `formats` 里那几组
（54 种），不在这里抄第二份清单 —— 抄了就会与抓取侧的判定分叉（同一个 `.ts` 在一边是源码、
在另一边是视频）。

**安全边界（与 `py.exec` 同一套思路，只是更窄）**：
只允许 ``data/``（含 ``data/exports/``）与会话工作目录之下；相对路径以会话工作目录为基准，
**绝对路径与 `..` 一律 ``realpath`` 之后用 ``commonpath`` 校验**；符号链接指向牢笼外同样拦。
判据是**解析后的真实路径**，不是入参字符串 —— `data/../../etc/passwd` 与
`data/link-out` 这两种写法在字符串上都很"清白"。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from scholarscout import paths
from scholarscout.fetch import formats, office
from scholarscout.fetch.html_text import extract_html
from scholarscout.fetch.pdf_text import pdf_text
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.tools.envelope import ToolResult, fail, ok

#: 目录列项上限（超出标 ``truncated``）：一个 `node_modules` 就能把上下文撑爆
MAX_LIST_ITEMS = 200
#: 递归深度上限（1 = 只列本层）：防「列个家目录」这种把整棵子树读进来的调用
DEFAULT_DEPTH = 1
MAX_DEPTH = 3
#: `file.read` 的行数口径：默认 400 行、上限 2000 行
DEFAULT_LIMIT = 400
MAX_LIMIT = 2000
#: 单次返回的字节硬上限（200KB）：行数没超、但每行都很长时靠它兜底
MAX_BYTES = 200 * 1024

#: 支持的类型组 → 后缀集合（后缀集合本身来自 `fetch/formats.py`，**不另抄**）。
#: `html` 单独一组：它要走正文提取器而不是原样解码。
SUPPORTED_GROUPS: dict[str, frozenset[str]] = {
    "text": frozenset(formats.TEXT_SUFFIXES),
    "pdf": frozenset({"pdf"}),
    "office": frozenset(formats.OFFICE_SUFFIXES),
    "html": frozenset({"html", "htm", "xhtml"}),
    "ebook": frozenset(formats.EBOOK_SUFFIXES),
    "rtf": frozenset(formats.RTF_SUFFIXES),
}

#: 给模型看的组说明（`unsupported_type` 时原样回给它，让它知道该换什么办法）
GROUP_HINTS: dict[str, str] = {
    "text": "文本（md/txt/py/json/yaml/csv/代码…，原样解码）",
    "pdf": "PDF（pymupdf 抽文字层，扫描版抽不出）",
    "office": "Office（docx/xlsx/pptx；.doc/.xls/.ppt 老格式需先转换）",
    "html": "网页（html/htm/xhtml，去标签留正文）",
    "ebook": "电子书（epub，按章抽字）",
    "rtf": "RTF（剥离控制字）",
}

#: 相对路径里可以剥掉的、与数据根同名的前导分量（`data/exports/a.md` 与 `exports/a.md` 等价）
_DATA_PREFIX = "data"


def _session_root(ctx: ToolContext) -> Path:
    """会话工作目录；没有就用 ``data/``。

    会话工作目录由上层通过 ``ctx.extra["workdir"]`` 注入（与 `py.exec` 的 workdir 同一概念）。
    相对路径、以及**没有 path 时的默认列目录根**都以它为准 —— 模型说"看看当前目录"，
    指的应当是这一轮真正在用的那个目录。
    """
    raw = (ctx.extra or {}).get("workdir")
    if isinstance(raw, str) and raw.strip():
        candidate = Path(raw.strip()).expanduser()
        if candidate.is_absolute():
            return Path(os.path.realpath(candidate))
    return Path(os.path.realpath(paths.data_dir()))


def _allowed_roots(ctx: ToolContext) -> list[Path]:
    """牢笼：``data/``（含 ``data/exports/``）与**会话工作目录**。

    会话工作目录若本来就在 ``data/`` 之下，不重复登记（判据相同，重复只会让错误文案变长）。
    """
    roots = [Path(os.path.realpath(paths.data_dir()))]
    session = _session_root(ctx)
    if session not in roots:
        roots.append(session)
    return roots


def _inside(candidate: Path, root: Path) -> bool:
    """``candidate`` 是否在 ``root`` 之下（都按**真实路径**比，符号链接已经展开）。"""
    try:
        common = os.path.commonpath([str(candidate), str(root)])
    except ValueError:
        # Windows 上不同盘符（C: 与 D:）没有公共前缀 —— 那就是越界，不是异常
        return False
    return Path(common) == root


def _display(path: Path) -> str:
    """给人看的路径：在项目根之下就写相对 POSIX 形式，否则写绝对路径（测试/临时目录常见）。"""
    try:
        return paths.rel_to_home(path)
    except ValueError:
        return path.as_posix()


def _strip_data_prefix(base: Path, relative: Path) -> Path:
    """相对路径里重复写 ``data/`` 不算错（照 `py_exec._under_root` 的口径）。

    模型的自然写法是 ``data/exports/a.md``，而相对基准已经是 ``data/`` —— 不剥就会得到
    ``data/data/exports/a.md``，然后报一个"文件不存在"，让它以为文件没了。
    """
    parts = relative.parts
    while parts and parts[0].lower() == _DATA_PREFIX:
        parts = parts[1:]
    return base.joinpath(*parts) if parts else base


def _resolve(ctx: ToolContext, raw: str | None) -> tuple[Path | None, str, str]:
    """把入参收进牢笼。返回 ``(真实路径, 错误码, 错误文案)``，三者只有一组非空。

    顺序是**先规范化再判定**：``expanduser`` → 相对则拼基准根（并剥 ``data`` 前缀）→
    ``os.path.realpath``（等价 POSIX 的 realpath：展开符号链接、吃掉 ``..``）→
    ``commonpath`` 与每个允许根比对。所以 ``..``、绝对路径、符号链接三种越界走的是**同一条**判据。
    """
    text = (raw or "").strip()
    if not text:
        return None, "invalid_path", "path 不能为空：要给一个 data/ 或会话工作目录下的路径"
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = _strip_data_prefix(_session_root(ctx), candidate)
    resolved = Path(os.path.realpath(candidate))
    roots = _allowed_roots(ctx)
    if not any(_inside(resolved, root) for root in roots):
        allowed = "、".join(_display(root) for root in roots)
        return (
            None,
            "outside_allowed_root",
            f"只能读 {allowed} 之下的路径；收到的是 {_display(resolved)}。"
            "这个工具是只读且被路径牢笼限制的：越界的路径一律不打开，"
            "不要把同一个目标换个写法（..、绝对路径、符号链接）再试。",
        )
    return resolved, "", ""


def _as_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    """参数收敛：非数字回默认值，超出范围夹住（不抛异常 —— 参数越界是正常调用噪声）。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(number, maximum))


def _decode(raw: bytes) -> str:
    """文本解码：utf-8 → gbk → latin-1（latin-1 任何字节都能解，是最后的兜底）。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _detect_group(suffix: str) -> str:
    """后缀 → 类型组名；不认识回空串。"""
    for group, suffixes in SUPPORTED_GROUPS.items():
        if suffix in suffixes:
            return group
    return ""


def _unsupported(ctx: ToolContext, target: Path, suffix: str) -> ToolResult:
    """不支持的类型：**列清支持哪些组**（可读错误，不是 500）。"""
    groups = [
        {"name": name, "hint": GROUP_HINTS[name], "suffixes": sorted(SUPPORTED_GROUPS[name])}
        for name in SUPPORTED_GROUPS
    ]
    hint = "；".join(f"{name}={GROUP_HINTS[name]}" for name in SUPPORTED_GROUPS)
    # 老格式（.doc/.xls/.ppt）是"看起来支持其实读不了"的典型：给能照做的下一步，别说"不支持"三个字
    extra = formats.legacy_office_reason(suffix) if suffix in formats.LEGACY_OFFICE_SUFFIXES else ""
    message = f"不支持读取 .{suffix or '(无后缀)'}：这个只读工具能打开的类型组是 {hint}。"
    if extra:
        message = f"{message}{extra}；"
    message += (
        "压缩包（zip/tar*）与音视频不抽正文；要处理其它格式请用 source.fetch（联网抓取）"
        "或 py.exec（需要用户确认）。"
    )
    return fail(
        "unsupported_type",
        message,
        data={
            "kind": "unsupported_type",
            "path": _display(target),
            "extension": suffix,
            "groups": groups,
        },
    )


def _extract(ctx: ToolContext, target: Path, group: str, suffix: str) -> tuple[str, str, str]:
    """按类型组路由到**现有抽取器**。返回 ``(正文, 引擎, 错误文案)``，错误文案非空即失败。

    每个分支都调用下层已有的函数，本模块**不做任何解析**：
    ``pdf`` → `fetch.pdf_text.pdf_text`；``office`` → `fetch.office.office_text`；
    ``html`` → `fetch.html_text.extract_html`；``ebook``/``rtf`` → `fetch.formats`；
    ``text`` → 就地解码（`fetch/formats` 里这组的口径就是"原样解码，不要洗"）。
    """
    raw = target.read_bytes()
    if group == "text":
        return _decode(raw), "decode", ""
    if group == "pdf":
        text, status, engine, detail = pdf_text(
            raw, budget_s=ctx.config.fetch_cfg.extract_timeout_s
        )
        if not text:
            return "", engine, f"PDF 抽不出正文（{status}）：{detail}"
        return text, engine, ""
    if group == "office":
        extracted, text, detail = office.office_text(suffix, raw)
        if not extracted:
            return "", "office", f"Office 抽取失败：{detail}"
        return text, "office", ""
    if group == "html":
        return extract_html(_decode(raw)).text, "html", ""
    if group == "ebook":
        extracted, text, detail = formats.epub_text(raw)
        if not extracted:
            return "", "ebook", f"epub 抽取失败：{detail}"
        return text, "ebook", ""
    if group == "rtf":
        extracted, text, detail = formats.rtf_text(raw)
        if not extracted:
            return "", "rtf", f"rtf 抽取失败：{detail}"
        return text, "rtf", ""
    return "", "none", f"没有对应的抽取器：{group}"


def _entry(path: Path, root: Path) -> dict[str, Any]:
    """一条目录项：``name`` 是**相对列目录根**的 POSIX 路径（嵌套项才不会重名）。"""
    is_dir = path.is_dir()
    size = 0 if is_dir else path.stat().st_size
    return {
        "name": path.relative_to(root).as_posix(),
        "is_dir": is_dir,
        "bytes": size,
        "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
    }


def file_list(ctx: ToolContext, path: str | None = None, depth: int | None = None) -> ToolResult:
    """列目录（只读）：目录在前、文件在后，最多 :data:`MAX_LIST_ITEMS` 项。"""
    root, code, message = _resolve(ctx, path) if path else (_session_root(ctx), "", "")
    if root is None:
        return fail(code, message, data={"kind": code})
    if not root.exists():
        return fail(
            "not_found",
            f"目录不存在：{_display(root)}（只读工具不会创建它）",
            data={"kind": "not_found", "path": _display(root)},
        )
    if not root.is_dir():
        return fail(
            "not_a_directory",
            f"{_display(root)} 是文件不是目录；读内容请用 file.read",
            data={"kind": "not_a_directory", "path": _display(root)},
        )

    level_cap = _as_int(depth, default=DEFAULT_DEPTH, minimum=1, maximum=MAX_DEPTH)
    collected: list[dict[str, Any]] = []
    truncated = False
    # 广度优先逐层展开；**不跟随符号链接目录**（否则一个指回上层的链接就能把遍历卡死）
    frontier: list[tuple[Path, int]] = [(root, 1)]
    while frontier and not truncated:
        current, level = frontier.pop(0)
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            # 单个子目录不可读（权限/占用）不该让整次列目录失败：跳过它，其余照列
            continue
        for child in children:
            if len(collected) >= MAX_LIST_ITEMS:
                truncated = True
                break
            try:
                collected.append(_entry(child, root))
            except OSError:
                continue
            if child.is_dir() and not child.is_symlink() and level < level_cap:
                frontier.append((child, level + 1))
    collected.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
    return ok(
        {
            "path": _display(root),
            "kind": "dir",
            "depth": level_cap,
            "count": len(collected),
            "truncated": truncated,
            "entries": collected,
        }
    )


def file_read(
    ctx: ToolContext,
    path: str,
    offset: int | None = None,
    limit: int | None = None,
) -> ToolResult:
    """读文件（只读）：按扩展名复用抽取器，回**带行号**的分页正文。"""
    target, code, message = _resolve(ctx, path)
    if target is None:
        return fail(code, message, data={"kind": code})
    if not target.exists():
        return fail(
            "not_found",
            f"文件不存在：{_display(target)}（只读工具不会创建它）",
            data={"kind": "not_found", "path": _display(target)},
        )
    if target.is_dir():
        return fail(
            "is_directory",
            f"{_display(target)} 是目录；列目录请用 file.list",
            data={"kind": "is_directory", "path": _display(target)},
        )

    suffix = target.suffix.lower().lstrip(".")
    group = _detect_group(suffix)
    if not group:
        return _unsupported(ctx, target, suffix)

    try:
        text, engine, error = _extract(ctx, target, group, suffix)
    except OSError as exc:
        return fail(
            "read_failed",
            f"读不了 {_display(target)}：{type(exc).__name__}（{exc.strerror or exc}）",
            data={"kind": "read_failed", "path": _display(target)},
        )
    if error:
        return fail(
            "extract_failed",
            f"{_display(target)}：{error}",
            data={"kind": "extract_failed", "path": _display(target), "group": group},
        )

    all_lines = text.splitlines()
    total_lines = len(all_lines)
    start_index = min(
        _as_int(offset, default=1, minimum=1, maximum=2**31 - 1) - 1,
        total_lines,
    )
    line_cap = _as_int(limit, default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)

    lines: list[dict[str, Any]] = []
    used_bytes = 0
    cut_by: str | None = None
    for index in range(start_index, total_lines):
        if len(lines) >= line_cap:
            cut_by = "limit"
            break
        line = all_lines[index]
        cost = len(line.encode("utf-8")) + 1
        if used_bytes + cost > MAX_BYTES:
            cut_by = "bytes"
            break
        used_bytes += cost
        lines.append({"n": index + 1, "text": line})

    truncated = cut_by is not None
    next_offset = (lines[-1]["n"] + 1) if (truncated and lines) else None
    return ok(
        {
            "path": _display(target),
            "kind": group,
            "engine": engine,
            "chars": len(text),
            "bytes": target.stat().st_size,
            "total_lines": total_lines,
            "offset": lines[0]["n"] if lines else start_index + 1,
            "count": len(lines),
            "next_offset": next_offset,
            "truncated": truncated,
            "truncated_by": cut_by,
            "lines": lines,
        }
    )


DESCRIPTION_LIST = (
    "列一个目录（**只读**）：目录在前、文件在后，每项给 name（相对该目录）/ is_dir / bytes / mtime。"
    "默认列当前会话工作目录（没有就用 data/）；depth 1-3（默认 1，不递归更深）；"
    "最多 200 项，超出标 truncated=true。只能列 data/、data/exports/ 与会话工作目录之下 —— "
    "越界（..、绝对路径、指向外面的符号链接）一律回 ok=false + data.kind=outside_allowed_root。"
)

DESCRIPTION_READ = (
    "读一个文件（**只读**，不创建、不修改）：按扩展名自动选抽取器 —— 文本原样解码（utf-8→gbk），"
    "PDF 用 pymupdf 抽文字层，docx/xlsx/pptx 走 Office 抽取，html 去标签，epub/rtf 也有。"
    "回 data.lines=[{n, text}] 带**行号**（引用原文时写行号），offset 从 1 开始、limit 默认 400 行"
    "（上限 2000），单次不超过 200KB，超出截断并标 truncated=true 且给 next_offset。"
    "只允许 data/、data/exports/ 与会话工作目录之下；越界回 data.kind=outside_allowed_root，"
    "不支持的类型回 data.kind=unsupported_type（含已支持的组）。"
)

LIST_SPEC = ToolSpec(
    name="file.list",
    description=DESCRIPTION_LIST,
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "要列的目录；省略 = 当前会话工作目录（没有则 data/）。"
                    "相对路径以会话工作目录为基准，写 data/xxx 与 xxx 等价"
                ),
            },
            "depth": {
                "type": "integer",
                "description": f"递归层数 1-{MAX_DEPTH}，缺省 1（只列本层）",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    handler=file_list,
    toolset="files",
)

READ_SPEC = ToolSpec(
    name="file.read",
    description=DESCRIPTION_READ,
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "要读的文件；相对路径以会话工作目录为基准，写 data/xxx 与 xxx 等价"
                ),
            },
            "offset": {
                "type": "integer",
                "description": "起始行号（1 起，缺省 1）；续读用上一条回包里的 next_offset",
            },
            "limit": {
                "type": "integer",
                "description": f"最多返回多少行，缺省 {DEFAULT_LIMIT}，上限 {MAX_LIMIT}",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    handler=file_read,
    toolset="files",
)

SPECS: tuple[ToolSpec, ...] = (LIST_SPEC, READ_SPEC)


def register() -> None:
    """注册两个只读工具（**只有 `features.enable_files` 开着时** `register_tools` 才会调到这里）。"""
    from scholarscout.runtime import registry

    for spec in SPECS:
        registry.register(spec, replace=True)

"""所有落盘路径的唯一入口。

规则 §3.8：落盘路径必须经本模块拼接并断言在 ``data/`` 之下，防 ``../`` 逃逸。
其他模块**不许**自己拼 ``data/...`` 字符串。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: data/ 下允许出现的子目录（超出这个集合即视为越界）
DATA_SUBDIRS: tuple[str, ...] = ("raw", "text", "notes", "cache")

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
    """抽取出的全文纯文本：``data/text/<doc_id>.txt``。"""
    if not doc_id or "/" in doc_id or "\\" in doc_id or ".." in doc_id:
        raise ValueError(f"非法 doc_id：{doc_id!r}")
    return under_data(Path("text") / f"{doc_id}.txt", root)


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

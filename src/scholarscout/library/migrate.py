"""一次性迁移：**任意已知旧布局** → **当前布局**（docs/19 §3）。

旧布局不需要枚举：入库时写下的 ``documents.raw_path`` / ``text_path`` 就是"旧路径"，
迁移只做一件事 —— 把文件从它现在的位置搬到按当前布局算出来的位置，并把库里两列改过去。

三条硬口径：

1. **是移动不是复制** —— 先 ``rename`` 文件，再在**同一个事务**里更新 ``documents.raw_path`` /
   ``text_path``；库更新失败就把文件改回去并如实报告（不留下「库里说在这、盘上却在别处」的错位）。
2. **幂等** —— 库里已指向新路径的条目直接跳过，重跑不产生任何副作用。
3. **冲突不丢数据** —— 目标位置已被别的文件占用时，给新名追加 ``--2``、``--3``…并在报告里列出。

**布局是参数**（``layout``）：默认 ``paths.DEFAULT_LAYOUT``（``<会话>/<格式>``），由配置
``library.layout`` 决定；同一份代码因此能把资料从任意旧布局迁到任意新布局（用户以后再改布局，
只要改配置再跑一次这个脚本）。

旧目录清理：把 ``data/raw`` / ``data/text`` 下**未被库引用的**历史文件移进 ``_orphan/``
（隔离而非删除 —— 那些文件已无元数据，编不出人类可读的名字，删了就是数据丢失），
随后**自底向上删掉空掉的旧目录**（``<类型>/<年-月>/``、``<2位>/``、老命名遗留的顶层文件所在目录）。

两条读查询（全库资料行 + 提及行）**直接用 `state/library_index.py` 的那两个函数**，不再抄第二份：
索引重建（`library/index.py`）要的列与迁移要的列本来就是同一批（``raw_path`` / ``text_path`` /
``sha256`` / ``fetched_at`` / 来源侧标题与 ``meta_json``），排序口径也一样
（``fetched_at DESC, rowid DESC`` / ``rowid`` 升序）—— 抄一份出去，两处就有机会悄悄分叉，
而那正是"索引与迁移重建结果逐字节一致"（docs/19 §2）最怕的事。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scholarscout import paths
from scholarscout.library import naming
from scholarscout.state import library_index as state_library_index
from scholarscout.state.archive import set_document_paths
from scholarscout.state.db import Database

#: 未引用历史文件的隔离目录名（放在各 data 子目录下）。
ORPHAN_DIR = "_orphan"

#: 会被清理（隔离孤儿 + 删空目录）的 data 子目录。
CLEANED_SUBDIRS: tuple[str, ...] = ("raw", "text")

#: 旧布局的目录名形状（清理时按名字把扫描限制在旧布局上，见 :func:`_old_layout_dirs`）：
#: ``<sha 前 2 位>``（最老的一版）与 ``<年-月>`` 桶。类型目录名直接用现布局的“格式”段（同源）。
_SHA_PREFIX_RE = re.compile(r"^[0-9a-fA-F]{2}$")
_MONTH_BUCKET_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_OLD_TYPE_DIRS = frozenset(paths.DOC_TYPE_BY_SOURCE.values()) | {"other"}


@dataclass(frozen=True)
class Move:
    """一条资料的计划：从旧路径到新路径。``kind ∈ rename | skip | conflict``。"""

    doc_id: str
    old_raw: str
    new_raw: str
    old_text: str
    new_text: str
    kind: str


@dataclass
class MigrationReport:
    """迁移结果。四个计数（改名 / 跳过 / 冲突 / 失败）互不重叠，相加 = 资料总条数。"""

    renamed: int = 0
    skipped: int = 0
    conflicts: int = 0
    failed: int = 0
    orphans: int = 0
    layout: str = ""
    moves: list[Move] = field(default_factory=list)
    conflict_items: list[dict[str, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    removed_dirs: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"改名 {self.renamed} ｜ 跳过 {self.skipped} ｜ "
            f"冲突 {self.conflicts} ｜ 失败 {self.failed}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "renamed": self.renamed,
            "skipped": self.skipped,
            "conflicts": self.conflicts,
            "failed": self.failed,
            "orphans": self.orphans,
            "layout": self.layout,
            "total": len(self.moves),
            "conflict_items": list(self.conflict_items),
            "failures": list(self.failures),
            "removed_dirs": list(self.removed_dirs),
            "moves": [
                {
                    "doc_id": m.doc_id,
                    "old_raw": m.old_raw,
                    "new_raw": m.new_raw,
                    "old_text": m.old_text,
                    "new_text": m.new_text,
                    "kind": m.kind,
                }
                for m in self.moves
            ],
        }


def _ext_of(path_text: str) -> str:
    name = (path_text or "").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _ext_from_url(url: str) -> str:
    tail = url.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[-1].lower() if "." in tail else ""


def _meta_of(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def earliest_sessions(db: Database) -> dict[str, str]:
    """``{source_id: 最早那次会话的 id}`` —— 目录取它（一份资料可能被多次调研带出）。

    「最早」是**写入顺序**（``source_mentions.rowid`` 升序），不是 ``created_at`` —— 与
    `state/archive.py::list_documents` 的分组口径、`library/index.py` 的会话列同一套。
    ``setdefault`` 就是「第一次见到的那条赢」，所以这里只依赖查询给的次序，不自己排序。
    """
    result: dict[str, str] = {}
    for row in state_library_index.source_mentions(db):
        result.setdefault(str(row["source_id"]), str(row["session_id"]))
    return result


def _collision_suffix(base: Path, old_raw: str, raw_abs: Path) -> str:
    """目标已存在且不是同一个文件时才冲突 → 返回 ``--2`` 之类的后缀，否则空串。"""
    old_abs = (base / old_raw).resolve() if old_raw else None
    if old_abs is not None and old_abs == raw_abs.resolve():
        return ""
    if not raw_abs.exists():
        return ""
    if old_abs is None or not old_abs.exists():
        # 目标在、源不在：多半是上次只改了名没更新库 → 当作同一个文件，不追加后缀
        return ""
    index = 2
    while True:
        candidate = raw_abs.with_name(f"{raw_abs.stem}--{index}{raw_abs.suffix}")
        if not candidate.exists():
            return f"--{index}"
        index += 1


def plan_moves(
    db: Database,
    *,
    root: Path | None = None,
    layout: str | tuple[str, ...] | None = None,
) -> list[Move]:
    """算出每条资料的「旧 → 新」计划；**不碰盘、不写库**（``--dry-run`` 用的就是它）。"""
    base = (root or paths.home()).resolve()
    segments = paths.normalize_layout(layout)
    session_dirs = naming.session_dir_map(db)
    earliest = earliest_sessions(db)
    moves: list[Move] = []
    for row in state_library_index.documents_with_sources(db):
        old_raw = str(row["raw_path"] or "")
        old_text = str(row["text_path"] or "")
        sha256 = str(row["sha256"])
        url = str(row["url"] or "")
        meta = _meta_of(row["meta_json"])
        domain = str(row["domain"] or "") or naming.domain_of(url)
        source_type = str(row["source_type"] or "")
        fmt = paths.doc_type_for(source_type)
        ext = _ext_of(old_raw) or _ext_from_url(url) or "bin"
        title = naming.pick_title(row["doc_title"], row["source_title"])
        collected = naming.collected_month(row["fetched_at"])
        published = naming.year_month_for(url, meta)
        session = session_dirs.get(earliest.get(str(row["source_id"]), ""), paths.UNKNOWN_SESSION)
        placement = {
            "session": session,
            "fmt": fmt,
            "collected": collected,
            "published": published,
            "title": title,
            "sha256": sha256,
            "domain": domain,
        }

        raw_abs = paths.raw_path_for_layout(segments, ext=ext, root=base, **placement)
        text_abs = paths.text_path_for_layout(segments, root=base, **placement)
        suffix = _collision_suffix(base, old_raw, raw_abs)
        if suffix:
            raw_abs = raw_abs.with_name(f"{raw_abs.stem}{suffix}{raw_abs.suffix}")
            text_abs = text_abs.with_name(f"{text_abs.stem}{suffix}{text_abs.suffix}")

        new_raw = paths.rel_to_home(raw_abs, base)
        new_text = paths.rel_to_home(text_abs, base)
        if old_raw == new_raw and old_text == new_text:
            kind = "skip"
        elif suffix:
            kind = "conflict"
        else:
            kind = "rename"
        moves.append(Move(str(row["doc_id"]), old_raw, new_raw, old_text, new_text, kind))
    return moves


def _update_paths(db: Database, move: Move) -> None:
    """把这条移动的落点写进库（SQL 在 ``state.archive``；这里只做参数搬运，留作测试的缝）。"""
    set_document_paths(db, move.doc_id, raw_path=move.new_raw, text_path=move.new_text)


def _rename_pair(base: Path, move: Move) -> list[tuple[Path, Path]]:
    """移动原件与全文；返回已完成的 (旧, 新) 对，供失败时回滚。"""
    done: list[tuple[Path, Path]] = []
    for old_rel, new_rel in ((move.old_raw, move.new_raw), (move.old_text, move.new_text)):
        if not old_rel or not new_rel or old_rel == new_rel:
            continue
        old_abs = base / old_rel
        new_abs = base / new_rel
        if old_abs.exists():
            new_abs.parent.mkdir(parents=True, exist_ok=True)
            old_abs.rename(new_abs)
            done.append((old_abs, new_abs))
        elif not new_abs.exists():
            raise FileNotFoundError(f"既没有旧文件也没有目标文件：{old_rel}")
    return done


def _rollback(done: list[tuple[Path, Path]]) -> None:
    for old_abs, new_abs in reversed(done):
        try:
            if new_abs.exists() and not old_abs.exists():
                old_abs.parent.mkdir(parents=True, exist_ok=True)
                new_abs.rename(old_abs)
        except OSError:  # pragma: no cover - 回滚尽力而为，不掩盖原始失败
            pass


def _move_to_orphan(item: Path, orphan_dir: Path) -> None:
    orphan_dir.mkdir(parents=True, exist_ok=True)
    dest = orphan_dir / item.name
    index = 2
    while dest.exists():
        dest = orphan_dir / f"{item.stem}--{index}{item.suffix}"
        index += 1
    shutil.move(str(item), str(dest))


def _protected(base: Path, moves: list[Move]) -> set[Path]:
    """任何计划涉及的路径（含失败后留在原地的旧路径）都受保护，不被当孤儿隔离。"""
    keep: set[Path] = set()
    for move in moves:
        for rel in (move.old_raw, move.new_raw, move.old_text, move.new_text):
            if rel:
                keep.add((base / rel).resolve())
    return keep


def _prune_dirs(dirs: list[Path], root: Path, base: Path, report: MigrationReport) -> None:
    """自底向上删掉扫过的空目录（``root`` 自身保留；旧桶目录只在空了之后才留不住）。"""
    for directory in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        if directory == root or any(directory.iterdir()):
            continue
        rel = paths.rel_to_home(directory, base)
        directory.rmdir()
        report.removed_dirs.append(rel)


def _looks_like_old_level1(name: str) -> bool:
    """旧布局第一层：``<sha 前 2 位>`` 目录，或旧的内容类型目录（也可能直接是 ``unknown``）。"""
    return (
        bool(_SHA_PREFIX_RE.match(name)) or name in _OLD_TYPE_DIRS or name == paths.UNKNOWN_BUCKET
    )


def _looks_like_old_bucket(name: str) -> bool:
    """旧布局第二层：``<年-月>`` 桶或 ``unknown``。"""
    return bool(_MONTH_BUCKET_RE.match(name)) or name == paths.UNKNOWN_BUCKET


def _old_layout_dirs(root: Path) -> list[Path]:
    """``root`` 下**已知旧布局形状**的目录（含 ``root`` 自身，由浅到深）。

    **为什么不用 ``rglob('*')``**：当前布局是 ``<会话>/<格式>/``（文件在第三层），资料一多
    整棵 ``data/raw`` 走一遍纯属浪费 —— 要清的只是**旧布局残留**，而旧布局的目录名是可枚举的：
    ``<sha 前 2 位>/``、``<类型>/<年-月>/``、``<类型>/unknown/``。只按名字下探这两类形状，
    **新布局的会话目录一律不进**（跳过它就是省下来的时间）。
    """
    dirs = [root]
    for level1 in sorted(p for p in root.iterdir() if p.is_dir()):
        if level1.name == ORPHAN_DIR or not _looks_like_old_level1(level1.name):
            continue
        dirs.append(level1)
        for level2 in sorted(p for p in level1.iterdir() if p.is_dir()):
            if level2.name == ORPHAN_DIR or not _looks_like_old_bucket(level2.name):
                continue
            dirs.append(level2)
    return dirs


def _cleanup_old_dirs(base: Path, moves: list[Move], report: MigrationReport) -> None:
    """隔离未被库引用的历史文件，并删掉空掉的旧目录（布局换过之后旧桶不该继续挂着）。

    扫描范围**限定在旧布局形状**（见 :func:`_old_layout_dirs`），不 ``rglob`` 整棵 ``data/raw``。
    """
    protected = _protected(base, moves)
    for name in CLEANED_SUBDIRS:
        root = base / "data" / name
        if not root.is_dir():
            continue
        dirs = _old_layout_dirs(root)
        for directory in dirs:
            for item in sorted(directory.iterdir()):
                if item.is_file() and item.resolve() not in protected:
                    _move_to_orphan(item, root / ORPHAN_DIR)
                    report.orphans += 1
        _prune_dirs(dirs, root, base, report)


def run_migration(
    db: Database,
    *,
    root: Path | None = None,
    dry_run: bool = False,
    layout: str | tuple[str, ...] | None = None,
) -> MigrationReport:
    """执行迁移（或 ``dry_run`` 只出计划）。返回带四个计数与失败明细的报告。"""
    base = (root or paths.home()).resolve()
    segments = paths.normalize_layout(layout)
    moves = plan_moves(db, root=base, layout=segments)
    report = MigrationReport(moves=moves, layout="/".join(segments))

    for move in moves:
        if move.kind == "skip":
            report.skipped += 1
        elif move.kind == "conflict":
            report.conflicts += 1
            report.conflict_items.append(
                {"doc_id": move.doc_id, "new_raw": move.new_raw, "new_text": move.new_text}
            )
        else:
            report.renamed += 1

    if dry_run:
        return report

    report.orphans = 0
    report.removed_dirs = []
    for move in moves:
        if move.kind == "skip":
            continue
        done: list[tuple[Path, Path]] = []
        try:
            done = _rename_pair(base, move)
            _update_paths(db, move)
        except Exception as exc:  # 逐条隔离：一条失败不影响其余
            _rollback(done)
            report.failed += 1
            report.failures.append(f"{move.doc_id}: {type(exc).__name__}: {exc}")
            if move.kind == "rename":
                report.renamed -= 1
            elif move.kind == "conflict":
                report.conflicts -= 1
                report.conflict_items = [
                    c for c in report.conflict_items if c["doc_id"] != move.doc_id
                ]

    _cleanup_old_dirs(base, moves, report)
    return report

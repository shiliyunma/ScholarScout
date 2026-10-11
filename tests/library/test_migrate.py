"""迁移：dry-run 不落盘、正式跑库盘一致、幂等、冲突后缀、失败回滚、旧目录清理（docs/19 §3/§5）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout import paths
from scholarscout.library import migrate
from scholarscout.state.db import Database

from ..conftest import T0

SHA = "ac4d3d6e" + "0" * 56
#: 最老的一版布局：``data/raw/<sha 前 2 位>/<sha>.<ext>``、``data/text/<doc_id>.txt``。
OLD_RAW = f"data/raw/{SHA[:2]}/{SHA}.html"
OLD_TEXT = "data/text/doc_old1.txt"
#: 当前布局（默认 ``<会话>/<格式>``）：会话目录名取标题 slug（"调研一"）。
NEW_RAW = "data/raw/调研一/paper/A-Vision-Model--ac4d3d6e.html"
NEW_TEXT = "data/text/调研一/paper/A-Vision-Model--ac4d3d6e.txt"


def _seed(db: Database, *, session: bool = True) -> None:
    db.execute(
        "INSERT INTO sources (id, url, url_canonical, domain, title, source_type,"
        " first_seen_at, last_seen_at, meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "src_a",
            "https://arxiv.org/abs/2410.24164",
            "https://arxiv.org/abs/2410.24164",
            "arxiv.org",
            "A Vision Model",
            "arxiv",
            T0,
            T0,
            "{}",
        ),
    )
    db.execute(
        "INSERT INTO documents (id, source_id, title, sha256, chars, raw_path, text_path,"
        " extract_status, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("doc_old1", "src_a", "A Vision Model", SHA, 10, OLD_RAW, OLD_TEXT, "ok", T0),
    )
    if session:
        # 会话目录名取标题 slug；mention 的 rowid 决定"最早那次"（迁移目录取它）
        db.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
            ("ses_m1", "调研一", T0, T0),
        )
        db.execute(
            "INSERT INTO source_mentions (source_id, session_id, created_at) VALUES (?,?,?)",
            ("src_a", "ses_m1", T0),
        )


def _place(root: Path, rel: str, payload: str = "x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


def _db_path(db: Database, column: str) -> str | None:
    return db.scalar_str(f"SELECT {column} FROM documents WHERE id = 'doc_old1'")


def test_dry_run_plans_without_touching_disk_or_db(db: Database, tmp_path: Path) -> None:
    _seed(db)
    _place(tmp_path, OLD_RAW)
    _place(tmp_path, OLD_TEXT, "正文")

    report = migrate.run_migration(db, root=tmp_path, dry_run=True)

    assert report.renamed == 1
    assert report.skipped == 0
    assert report.moves[0].new_raw == NEW_RAW
    assert report.moves[0].new_text == NEW_TEXT
    assert report.layout == "session/format"
    assert (tmp_path / OLD_RAW).is_file(), "dry-run 不许动盘"
    assert not (tmp_path / NEW_RAW).exists()
    assert _db_path(db, "raw_path") == OLD_RAW, "dry-run 不许写库"


def test_real_run_moves_files_and_updates_db(db: Database, tmp_path: Path) -> None:
    _seed(db)
    _place(tmp_path, OLD_RAW)
    _place(tmp_path, OLD_TEXT, "正文")

    report = migrate.run_migration(db, root=tmp_path)

    assert report.renamed == 1
    assert report.failed == 0
    assert (tmp_path / NEW_RAW).is_file()
    assert (tmp_path / NEW_TEXT).is_file()
    assert not (tmp_path / OLD_RAW).exists(), "旧路径必须删掉（是移动不是复制）"
    assert not (tmp_path / OLD_TEXT).exists()
    assert _db_path(db, "raw_path") == NEW_RAW
    assert _db_path(db, "text_path") == NEW_TEXT


def test_rerun_is_idempotent(db: Database, tmp_path: Path) -> None:
    _seed(db)
    _place(tmp_path, OLD_RAW)
    _place(tmp_path, OLD_TEXT)

    migrate.run_migration(db, root=tmp_path)
    second = migrate.run_migration(db, root=tmp_path)

    assert (second.renamed, second.skipped, second.conflicts, second.failed) == (0, 1, 0, 0)
    assert (tmp_path / NEW_RAW).is_file()


def test_layout_parameter_decides_target_dirs(db: Database, tmp_path: Path) -> None:
    """布局是**参数**：换个布局（``发表年月/格式``）就该落到另一套目录。"""
    _seed(db, session=False)  # 没有会话：发表的年月段才是这个用例的主角
    _place(tmp_path, OLD_RAW)
    _place(tmp_path, OLD_TEXT)

    report = migrate.run_migration(db, root=tmp_path, layout="published/format")

    assert report.layout == "published/format"
    assert (
        tmp_path / "data" / "raw" / "2024-10" / "paper" / "A-Vision-Model--ac4d3d6e.html"
    ).is_file()
    assert _db_path(db, "raw_path") == "data/raw/2024-10/paper/A-Vision-Model--ac4d3d6e.html"


def test_conflict_appends_suffix_and_is_reported(db: Database, tmp_path: Path) -> None:
    _seed(db)
    _place(tmp_path, OLD_RAW)
    _place(tmp_path, OLD_TEXT)
    _place(tmp_path, NEW_RAW, "别的文件占住了这个位置")

    report = migrate.run_migration(db, root=tmp_path)

    assert report.conflicts == 1
    assert report.renamed == 0
    suffixed = tmp_path / "data" / "raw" / "调研一" / "paper" / "A-Vision-Model--ac4d3d6e--2.html"
    assert suffixed.is_file()
    assert (
        report.conflict_items[0]["new_raw"]
        == "data/raw/调研一/paper/A-Vision-Model--ac4d3d6e--2.html"
    )
    assert _db_path(db, "raw_path") == paths.rel_to_home(suffixed, tmp_path)


def test_db_failure_rolls_files_back_and_reports(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db)
    _place(tmp_path, OLD_RAW)
    _place(tmp_path, OLD_TEXT)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("库写失败")

    monkeypatch.setattr(migrate, "_update_paths", boom)

    report = migrate.run_migration(db, root=tmp_path)

    assert report.failed == 1
    assert report.renamed == 0
    assert "RuntimeError" in report.failures[0]
    assert (tmp_path / OLD_RAW).is_file(), "库更新失败必须把文件改回去"
    assert not (tmp_path / NEW_RAW).exists()
    assert _db_path(db, "raw_path") == OLD_RAW


def test_unreferenced_files_quarantined_and_old_dirs_removed(db: Database, tmp_path: Path) -> None:
    _seed(db)
    _place(tmp_path, OLD_RAW)
    _place(tmp_path, OLD_TEXT)
    orphan_raw = _place(tmp_path, "data/raw/07/" + "7" * 64 + ".html")
    orphan_text = _place(tmp_path, "data/text/doc_orphan.txt")

    report = migrate.run_migration(db, root=tmp_path)

    assert report.orphans == 2
    assert (tmp_path / "data" / "raw" / migrate.ORPHAN_DIR / orphan_raw.name).is_file()
    assert (tmp_path / "data" / "text" / migrate.ORPHAN_DIR / orphan_text.name).is_file()
    # 旧 <2位> 目录清空并删除
    assert not (tmp_path / "data" / "raw" / "07").exists()
    assert not (tmp_path / "data" / "raw" / SHA[:2]).exists()
    assert "data/raw/07" in report.removed_dirs
    # 被引用的原件不能被当孤儿隔离
    assert not (tmp_path / "data" / "raw" / migrate.ORPHAN_DIR / f"{SHA}.html").exists()


def test_cleanup_only_scans_old_layout_shapes(db: Database, tmp_path: Path) -> None:
    """清理**只看旧布局形状**（``<2位>/``、``<类型>/<年-月>/``）—— 新布局的会话目录整棵跳过。

    这是**故意的口径**，不是漏扫：当前布局是 ``<会话>/<格式>/``，库一大会走不完；
    而新布局下的文件都是本轮/上次迁移的产物、必然被库引用，不需要扫。
    """
    _seed(db)
    _place(tmp_path, OLD_RAW)
    _place(tmp_path, OLD_TEXT)
    stray = _place(tmp_path, "data/raw/某会话/paper/a-stray.html")  # 会话目录名，不是旧布局形状
    old_orphan = _place(tmp_path, "data/raw/07/" + "7" * 64 + ".html")  # 旧布局形状 → 要被隔离

    report = migrate.run_migration(db, root=tmp_path)

    assert report.orphans == 1, "只隔离旧布局里的未引用文件"
    assert stray.is_file(), "新布局会话目录里的文件不归本次清理管"
    assert not old_orphan.exists()
    assert (tmp_path / "data" / "raw" / migrate.ORPHAN_DIR / old_orphan.name).is_file()

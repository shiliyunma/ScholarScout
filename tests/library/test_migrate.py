"""迁移：dry-run 不落盘、正式跑库盘一致、幂等、冲突后缀、失败回滚、旧目录清理（docs/19 §3/§5）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout import paths
from scholarscout.library import migrate
from scholarscout.state.db import Database

from ..conftest import T0

SHA = "ac4d3d6e" + "0" * 56
OLD_RAW = f"data/raw/{SHA[:2]}/{SHA}.html"
OLD_TEXT = "data/text/doc_old1.txt"
NEW_RAW = "data/raw/paper/2024-10/A-Vision-Model--ac4d3d6e.html"
NEW_TEXT = "data/text/paper/2024-10/A-Vision-Model--ac4d3d6e.txt"


def _seed(db: Database) -> None:
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


def test_conflict_appends_suffix_and_is_reported(db: Database, tmp_path: Path) -> None:
    _seed(db)
    _place(tmp_path, OLD_RAW)
    _place(tmp_path, OLD_TEXT)
    _place(tmp_path, NEW_RAW, "别的文件占住了这个位置")

    report = migrate.run_migration(db, root=tmp_path)

    assert report.conflicts == 1
    assert report.renamed == 0
    suffixed = tmp_path / "data" / "raw" / "paper" / "2024-10" / "A-Vision-Model--ac4d3d6e--2.html"
    assert suffixed.is_file()
    assert (
        report.conflict_items[0]["new_raw"]
        == "data/raw/paper/2024-10/A-Vision-Model--ac4d3d6e--2.html"
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

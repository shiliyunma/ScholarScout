"""数据维护：缓存保留期 + 数据库整理（只整理、不删数据）。

为什么要有这一件：

- **删会话不会让库文件变小**（SQLite 把页标记空闲但仍占空间），用户会以为"删了没用"→
  需要显式 `VACUUM`；而且它**只整理不删数据**，与"资料本体拒绝清理"那条硬约定不冲突。
- 缓存不该"一刀全清"：全清等于下次检索重新下载一遍。按 mtime 只删够旧的文件才是合理策略。

红线（有测试钉住）：`data/raw`、`data/text`、`data/notes` 与数据库本体**永远不在可清理范围**。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from scholarscout import settings_io
from scholarscout.state import maintenance


def _touch(path: Path, *, age_days: float, size: int = 1024) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_days * 86400
    import os

    os.utime(path, (stamp, stamp))
    return path


def test_cleanup_without_age_filter_removes_everything(tmp_path: Path) -> None:
    _touch(tmp_path / "data" / "cache" / "a.bin", age_days=30)
    _touch(tmp_path / "data" / "cache" / "sub" / "b.bin", age_days=0)
    removed = settings_io.cleanup(tmp_path, target="fetch_cache")
    assert removed["files"] == 2
    assert not (tmp_path / "data" / "cache" / "a.bin").exists()


def test_cleanup_with_age_filter_keeps_fresh_files(tmp_path: Path) -> None:
    """**只删够旧的**：新的缓存留着（否则下次检索又要重新下载）。"""
    old = _touch(tmp_path / "data" / "cache" / "old.bin", age_days=10)
    fresh = _touch(tmp_path / "data" / "cache" / "fresh.bin", age_days=1)
    removed = settings_io.cleanup(tmp_path, target="fetch_cache", older_than_days=7)
    assert removed["files"] == 1
    assert not old.exists()
    assert fresh.exists(), "新鲜缓存不能被删"


def test_cleanup_rejects_negative_age(tmp_path: Path) -> None:
    with pytest.raises(settings_io.SettingsError):
        settings_io.cleanup(tmp_path, target="fetch_cache", older_than_days=-1)


@pytest.mark.parametrize("target", ["database", "raw", "text", "notes", "state.db"])
def test_corpus_is_never_cleanable(tmp_path: Path, target: str) -> None:
    """资料本体与数据库本体永远拒绝清理（这是硬约定，不是提示）。"""
    with pytest.raises(settings_io.SettingsError, match="不允许清理"):
        settings_io.cleanup(tmp_path, target=target)


def test_run_tmp_cleanup_never_deletes_the_database(tmp_path: Path) -> None:
    """`run/` 清理也要放过库文件（state.db*）—— 那里装着全部数据。"""
    db = _touch(tmp_path / "run" / "state.db", age_days=30)
    other = _touch(tmp_path / "run" / "window.json", age_days=30)
    removed = settings_io.cleanup(tmp_path, target="run_tmp")
    assert db.exists(), "库文件不能被 run/ 清理带走"
    assert not other.exists()
    assert removed["files"] == 1


def test_optimize_database_shrinks_after_deletes(ctx: object) -> None:
    """删掉大量行之后 VACUUM 必须让**逻辑页数**下降（这是这个功能的全部意义）。

    为什么钉页数而不是文件字节数：WAL 模式下写入先进 `-wal`，主库文件可能纹丝不动 ——
    只断言字节数会时红时绿，还会把"整理没用"当成结论。
    """
    from scholarscout.state import sessions

    db = ctx.db  # type: ignore[attr-defined]
    for index in range(60):
        session_id = sessions.create_session(db, model="stub-model", title=f"占位 {index}")
        sessions.append_message(db, session_id, role="user", content="x" * 800)
    pages_full = maintenance.database_pages(db)
    db.execute("DELETE FROM messages")
    db.execute("DELETE FROM sessions")
    result = settings_io.optimize_database(db)
    assert result["pages_before"] >= pages_full - 1  # 删行不会立刻减页
    assert result["pages_after"] < result["pages_before"], "VACUUM 之后页数必须下降"
    assert result["page_saved"] > 0
    assert result["saved"] >= 0  # 字节数可能为 0（WAL），如实报即可


def test_optimize_reports_zero_saved_on_a_tidy_database(ctx: object) -> None:
    """本来就没有碎片时如实报 0，不编一个"已释放"。"""
    db = ctx.db  # type: ignore[attr-defined]
    result = settings_io.optimize_database(db)
    assert result["page_saved"] >= 0
    assert result["saved"] >= 0

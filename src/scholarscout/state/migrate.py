"""迁移执行器：把 ``migrations/NNNN_*.sql`` 按版本号依次应用。

幂等保证：已应用的版本记在 ``schema_version`` 表里，重复执行不会重跑。
原子性保证：每个迁移与它的版本登记在同一个事务里提交。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from scholarscout.state.db import Database

MIGRATION_FILE_RE = re.compile(r"^(\d{4})_[A-Za-z0-9_\-]+\.sql$")


def migrations_dir() -> Path:
    return Path(__file__).resolve().parent / "migrations"


def now_iso() -> str:
    """带本地时区偏移的 ISO-8601 时间字符串（项目统一的时间格式）。

    **带微秒**：同一秒内可能写入多条记录（例如一次判权多行），秒级精度会让
    ``ORDER BY created_at DESC`` 取到非确定的"最新一条"（踩过）。
    """
    return datetime.now(UTC).astimezone().isoformat(timespec="microseconds")


def discover(directory: Path | None = None) -> list[tuple[int, Path]]:
    """返回 [(版本号, 文件路径)]，按版本号升序。文件名不合规直接抛（不让它静默漏掉）。"""
    base = directory or migrations_dir()
    if not base.is_dir():
        raise FileNotFoundError(f"迁移目录不存在：{base}")
    found: list[tuple[int, Path]] = []
    for path in sorted(base.glob("*.sql")):
        match = MIGRATION_FILE_RE.match(path.name)
        if match is None:
            raise ValueError(f"迁移文件名不合规（应为 NNNN_描述.sql）：{path.name}")
        found.append((int(match.group(1)), path))
    versions = [v for v, _ in found]
    if len(set(versions)) != len(versions):
        raise ValueError(f"迁移版本号重复：{versions}")
    return sorted(found, key=lambda item: item[0])


def current_version(db: Database) -> int:
    """当前库版本；未初始化返回 0。"""
    if not db.has_table("schema_version"):
        return 0
    return db.scalar_int("SELECT max(version) FROM schema_version")


def apply_migrations(
    db: Database,
    *,
    target: int | None = None,
    directory: Path | None = None,
) -> list[int]:
    """应用迁移，返回本次实际应用的版本号列表。"""
    version = current_version(db)
    applied: list[int] = []
    for number, path in discover(directory):
        if number <= version:
            continue
        if target is not None and number > target:
            continue
        sql = path.read_text(encoding="utf-8")
        stamp = now_iso().replace("'", "''")
        # 迁移 + 版本登记同一事务；executescript 会先结束隐式事务，所以显式写 BEGIN/COMMIT
        script = (
            "BEGIN;\n"
            f"{sql}\n"
            f"INSERT INTO schema_version (version, applied_at) VALUES ({number}, '{stamp}');\n"
            "COMMIT;"
        )
        db.executescript(script)
        applied.append(number)
    return applied

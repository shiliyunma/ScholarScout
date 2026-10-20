"""数据维护：磁盘占用统计、缓存清理、库整理 —— 这些都不是"设置"。

为什么单独一层：`settings_io` 的职责是「把用户改的值校验后原子写回」；而这里的三个入口
读的全是**磁盘上的数据 home**（库 / 缓存 / raw / text / notes / run），既不读也不写
`config.yaml` / `.env`。混在 `settings_io` 里会让"设置 IO"看起来像在管数据，两边也都长到
没人愿意读。

为什么在 `state/` 而不是顶层：它只依赖 `paths` 与 `state/maintenance.py`（SQL 在那边），
属于存储层的「量体积 / 删缓存 / 整理库」，不是入口层。放顶层还要去改
`tests/test_layering.py` 的"每个顶层包都得在规则表里"那张表 —— 拆分不该为了搬家去改测试。

`SettingsError` 定义在这里（值 / 参数不合法，RPC 层翻成 -32602）：它同时被 `settings_io`
的字段校验和本模块的清理参数校验使用。放在**最底层**才没有 `settings_io` 与本模块之间的
循环 import（`settings_io` 在模块级 import 本模块，本模块若反向 import 它就会拿到半成品）；
`settings_io` 原样重导出，`settings_io.SettingsError` 仍是同一个类对象。

两条红线（有测试钉住）：可清理的**只有**缓存与 `run/` 下的临时文件；`data/raw`、`data/text`、
`data/notes` 与数据库本体永远拒绝清理。库整理（`optimize_database`）只整理、不删数据。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from scholarscout import paths
from scholarscout.state import maintenance


class SettingsError(ValueError):
    """设置值不合法（RPC 层会翻成 -32602 参数错误）。"""


def _dir_size(path: Path) -> tuple[int, int]:
    """（字节数, 文件数）。目录不存在就是 0。"""
    if not path.exists():
        return 0, 0
    total = 0
    count = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
            count += 1
    return total, count


def data_usage(root: Path | None = None) -> dict[str, dict[str, Any]]:
    """磁盘占用（设置栏的「数据」那一栏要显示的东西）。"""
    base = root or paths.home()
    targets: dict[str, Path] = {
        "database": paths.db_path(base),
        "fetch_cache": paths.data_dir(base) / "cache",
        "raw": paths.data_dir(base) / "raw",
        "text": paths.data_dir(base) / "text",
        "notes": paths.data_dir(base) / "notes",
        "run": paths.run_dir(base),
    }
    usage: dict[str, dict[str, Any]] = {}
    for name, path in targets.items():
        if path.is_file():
            size = path.stat().st_size
            usage[name] = {"bytes": size, "files": 1, "path": str(path)}
            continue
        size, count = _dir_size(path)
        usage[name] = {"bytes": size, "files": count, "path": str(path)}
    return usage


#: 允许清理的目标（**故意不含 raw / text / notes**：那些是资料本体，删了就没了）
CLEANABLE = {
    "fetch_cache": "抓取缓存（删了只是下次重新下载网页）",
    "run_tmp": "run/ 下的临时文件（诊断产物、临时库等）",
}


def cleanup(
    root: Path | None = None,
    target: str = "fetch_cache",
    *,
    older_than_days: float | None = None,
    now: float | None = None,
) -> dict[str, int]:
    """清理缓存类目录。返回删掉的（文件数, 字节数）。

    `older_than_days` 只删"够旧"的文件（按 mtime）：缓存的价值本来就随时间衰减，
    全清会让下次检索重新下载一遍。**资料本体永远不在可清理范围内**（见 CLEANABLE）。
    """
    base = root or paths.home()
    if target not in CLEANABLE:
        raise SettingsError(f"不允许清理：{target}（可清理的只有 {', '.join(CLEANABLE)}）")
    directory = paths.data_dir(base) / "cache" if target == "fetch_cache" else paths.run_dir(base)
    cutoff = None
    if older_than_days is not None:
        if older_than_days < 0:
            raise SettingsError("older_than_days 不能为负")
        cutoff = (now if now is not None else time.time()) - older_than_days * 86400
    files = 0
    size = 0
    for item in sorted(directory.rglob("*")):
        if not item.is_file():
            continue
        if target == "run_tmp" and item.name.startswith(("state.db", "scout.db")):
            continue  # 库不能删：那是全部数据
        if cutoff is not None and item.stat().st_mtime >= cutoff:
            continue  # 还"新鲜"，留着（缓存的价值随时间衰减，不是一滴不留）
        size += item.stat().st_size
        item.unlink(missing_ok=True)
        files += 1
    return {"files": files, "bytes": size}


def optimize_database(db: Any, root: Path | None = None) -> dict[str, int]:
    """整理数据库：`VACUUM` + `PRAGMA optimize`，返回整理前后的字节数与页数。

    为什么需要它：删会话/删资料**不会**让 SQLite 变紧凑（页被标记空闲但仍占空间），
    所以"删了一堆会话但磁盘没变"是正常的，得显式整理。

    口径：**只整理、不删除任何数据** —— 与"资料本体拒绝清理"这条硬约定不冲突；
    同时给出文件字节数与逻辑页数（WAL 下两者会不一致，只报一个就会误导）。
    代价：**大库会短暂锁库**（几秒到几十秒），期间其它请求可能排队（有 busy_timeout 兜着）。

    分工：SQL 在 `state/maintenance.py`（VACUUM / PRAGMA optimize / 页数），
    本函数只负责"整理前后各量一次、把差值讲给界面听"。SQL 收进 state 之后，
    "写库语句只在 state/ 里"是可 grep 的验收口径，不是约定俗成。
    """
    path = Path(db.path) if getattr(db, "path", None) is not None else paths.db_path(root)
    before = path.stat().st_size if path.is_file() else 0
    pages_before = maintenance.database_pages(db)
    maintenance.vacuum(db)
    maintenance.optimize(db)
    after = path.stat().st_size if path.is_file() else 0
    pages_after = maintenance.database_pages(db)
    return {
        "before": before,
        "after": after,
        "saved": max(0, before - after),
        "pages_before": pages_before,
        "pages_after": pages_after,
        "page_saved": max(0, pages_before - pages_after),
    }


__all__ = ["CLEANABLE", "SettingsError", "cleanup", "data_usage", "optimize_database"]

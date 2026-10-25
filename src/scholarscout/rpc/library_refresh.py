"""导出类 RPC 的「写完就入库」钩子 —— 与 ``py.exec`` 调的是**同一个**函数。

## 为什么要有这个文件

``py.exec`` 那条路修好之后（``library/artifacts.py``），"产物不入库"还剩一半：
``session.export`` / ``route.export`` / 渠道发文件这三处**也会往 ``data/exports/``（或
``data/notes/``）写文件**，但它们不经过 ``py.exec``。不接上就会出现新的不对称：

    模型写的 docx 在资料库里，用户点"导出"生成的 md 却不在 —— 同一个目录、同一类产物、两套结果。

所以钩子只做一件小事：把"哪个目录、哪个会话、布局怎么配"折成一次
:func:`scholarscout.library.artifacts.refresh_after_write` 调用。增量/幂等/失败隔离
的口径全在那个函数里，这里**不重复实现**（判据只有一处，才不会被改歪一半）。

## 失败口径

导出本身已经成功了（文件在盘上、路径已回给用户），索引失败**不许**让导出报错：
``refresh_after_write`` 自己就把异常收成 ``indexed=false``，这个钩子再兜一层
``except``，确保"附加动作"永远不会把主流程判成失败。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scholarscout import paths
from scholarscout.library import artifacts as artifact_index

if TYPE_CHECKING:  # 只为类型：运行期不 import，避免 rpc 内部循环
    from scholarscout.rpc.registry import AppContext

logger = logging.getLogger(__name__)

#: 导出目录名（与 ``rpc/channel_file.EXPORT_DIR``、``state/export_session`` 同口径）
EXPORT_DIR = "exports"


def exports_dir(workdir: Path | None = None) -> Path:
    """这次导出落在哪个目录（不给就是 ``<data>/exports``）。"""
    return Path(workdir) if workdir is not None else paths.data_dir() / EXPORT_DIR


def refresh_exports(
    ctx: AppContext, session_id: str = "", *, workdir: Path | None = None
) -> dict[str, Any]:
    """导出件写完 → 增量入库 + 刷新资料索引 + 推 ``library.updated``。

    返回 ``refresh_after_write`` 的回包（``indexed`` / ``index_error`` / ``artifacts`` /
    ``artifact_scan``）；任何失败都收在字段里，**不抛**。
    """
    try:
        return artifact_index.refresh_after_write(
            ctx.db,
            exports_dir(workdir),
            session_id=session_id or None,
            layout=ctx.config.library.layout,
            emit=ctx.emit,
        )
    except Exception as exc:  # 导出已经成功：索引只能算"没刷新"，不能让导出跟着失败
        logger.warning("导出件入库失败（workdir=%s）：%s", workdir, exc)
        return {
            "indexed": False,
            "index_error": f"{type(exc).__name__}: {exc}",
            "artifacts": [],
            "artifact_scan": {"added": 0, "updated": 0, "unchanged": 0, "refreshed": False},
        }

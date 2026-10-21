"""进程日志落盘：``<home>/run/logs/app.log``（10MB × 3 轮转 / 幂等 / 失败降级）。

为什么要有这个模块：打包后的用户说"没反应"时，stderr 是**看不见的**（双击 exe 没有控制台），
于是故障现场等于零。日志必须落在**用户拷得走**的地方：``paths.home()`` 打包后就是 exe 所在
目录，所以日志就在包旁边，用户能把 ``run/logs/app.log`` 直接发过来。

三条纪律（改这里之前先读）：

1. **绝不影响启动**：建目录 / 开文件失败只降级回 stderr，``setup_logging()`` 永不抛 ——
   为了记日志而让应用起不来是本末倒置。
2. **幂等**：重复调用不重复挂 handler、不重复写行。判据是 handler 上的标记（不是模块级布尔），
   所以 ``--desktop`` 里"入口再调一次"是安全的。
3. **渠道子进程不写这里**：它有 ``run/channels/<platform>.log``（``channels/supervisor.py`` 把
   子进程 stderr 重定向过去）。混进来会让"渠道日志"与"主进程日志"互相污染。

``session_id`` / ``turn_id`` 用 ``contextvars`` + ``Filter`` 统一补上，**不改任何日志调用点**：
:func:`bind_turn` 在跑轮次的工作线程里挂一次，之后该线程里所有 ``logger.info(...)`` 自带身份。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from collections.abc import Callable
from contextvars import ContextVar, Token
from pathlib import Path
from typing import IO

from scholarscout import paths

#: 单个日志文件上限 10MB，另有 3 份历史（``app.log.1`` … ``app.log.3``）
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 3

#: 覆盖日志级别的环境变量（``DEBUG`` / ``INFO`` / ``WARNING`` …）；缺省 INFO
ENV_LEVEL = "SCS_LOG_LEVEL"

#: 打在自家 handler 上的标记键：幂等判据。**不靠模块级布尔** —— 布尔会被 reload /
#: 多个入口各自 import 的路径绕过，handler 上的标记是跟着 handler 走的。
HANDLER_MARK = "_scs_logging_kind"

#: 行格式：时间 + 级别 + logger 名 + 轮次身份 + 消息
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [sid=%(session_id)s turn=%(turn_id)s] %(message)s"

#: 没有上下文时的占位。不写 ``None``：看日志的人会以为值就是 "None"
NO_VALUE = "-"

#: 这一轮属于哪个会话 / 哪一轮（由 :func:`bind_turn` 在工作线程里挂上）
session_id_var: ContextVar[str] = ContextVar("scs_session_id", default="")
turn_id_var: ContextVar[str] = ContextVar("scs_turn_id", default="")

_KIND_FILE = "file"
_KIND_CONSOLE = "console"


class _ContextFilter(logging.Filter):
    """把 contextvars 里的 ``session_id`` / ``turn_id`` 补到每条记录上。

    为什么用 Filter 而不是让调用方传 ``extra=``：日志调用点有几十处，逐个改签名既容易漏、
    又把"记日志"和"带身份"耦在一起。Filter 是 logging 的官方扩展点，一行不用改调用点。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 走 __dict__：LogRecord 没有声明这两个字段，setattr/setattr 常量名会被 ruff B010 拦
        record.__dict__["session_id"] = session_id_var.get() or NO_VALUE
        record.__dict__["turn_id"] = turn_id_var.get() or NO_VALUE
        return True


def log_path(home: Path | None = None) -> Path:
    """日志文件路径：``<home>/run/logs/app.log``（路径拼接的唯一入口在 :mod:`paths`）。"""
    return paths.app_log_path(home)


def setup_logging(
    *,
    home: Path | None = None,
    level: int | str | None = None,
    stream: IO[str] | None = None,
) -> Path | None:
    """挂上文件 + 控制台 handler（幂等），返回日志文件路径；文件不可用时返回 ``None``。

    - ``home``：测试注入的根目录；缺省走 :func:`paths.home`（打包后 = exe 所在目录）。
    - ``level``：显式级别；缺省读 ``SCS_LOG_LEVEL``，再缺省 ``INFO``。
    - ``stream``：控制台输出流；缺省 ``sys.stderr`` —— **控制台输出保留**，行为不变。

    **不抛**：日志本身出问题时只降级（看 :func:`_attach_file`），调用方不需要 try。
    """
    root = logging.getLogger()
    resolved = _resolve_level(level)
    root.setLevel(resolved)

    existing_file = _own(root, _KIND_FILE)
    if existing_file is None:
        target = _attach_file(root, home, resolved)
    else:
        existing_file.setLevel(resolved)  # 级别允许第二次调高/调低
        target = _handler_path(existing_file)

    console = _own(root, _KIND_CONSOLE)
    if console is None:
        root.addHandler(_build_handler(logging.StreamHandler(stream or sys.stderr), resolved))
    else:
        console.setLevel(resolved)
    return target


def bind_turn(session_id: str | None = None, turn_id: str | None = None) -> Callable[[], None]:
    """把这一轮的身份挂到**当前上下文**上，返回一个复原函数。

    用法（工作线程里，一行绑定一行复原，不改任何 ``logger.xxx`` 调用）::

        reset = bind_turn(session_id, turn_id)
        try:
            ...
        finally:
            reset()

    用 token 复原而不是 `set(None)`：嵌套/并发线程各自持有自己的上下文，
    复原成"上一层"比复原成空更不容易把外层身份抹掉。
    """
    session_token: Token[str] = session_id_var.set(str(session_id or "").strip())
    turn_token: Token[str] = turn_id_var.set(str(turn_id or "").strip())

    def _reset() -> None:
        turn_id_var.reset(turn_token)
        session_id_var.reset(session_token)

    return _reset


# --------------------------------------------------------------------- 内部


def _resolve_level(level: int | str | None) -> int:
    """级别：显式参数 > ``SCS_LOG_LEVEL`` > INFO；认不出来的值一律回落 INFO（不炸）。"""
    if isinstance(level, int):
        return level
    raw = str(level or os.environ.get(ENV_LEVEL, "") or "INFO").strip().upper()
    resolved = logging.getLevelName(raw)
    return resolved if isinstance(resolved, int) else logging.INFO


def _own(root: logging.Logger, kind: str) -> logging.Handler | None:
    """root 上已有的、本模块挂的那种 handler（幂等的判据）。"""
    for handler in root.handlers:
        if handler.__dict__.get(HANDLER_MARK) == kind:
            return handler
    return None


def _handler_path(handler: logging.Handler) -> Path | None:
    base = handler.__dict__.get("baseFilename")
    return Path(str(base)) if base else None


def _attach_file(root: logging.Logger, home: Path | None, level: int) -> Path | None:
    """建目录 + 开轮转文件；**任何失败都只降级**（打一行到 stderr，返回 None）。"""
    target = log_path(home)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            target,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
    except Exception as exc:  # 日志失败不许拦住启动（模块 docstring 纪律 1）；原因打到 stderr
        # 用 print 而不是 logger：走到这里说明日志设施本身不可用，它可能也报不出来
        print(
            f"[logging] 日志文件不可用，退回 stderr：{type(exc).__name__}: {exc}", file=sys.stderr
        )
        return None
    _build_handler(handler, level)
    root.addHandler(handler)
    return target


def _build_handler(handler: logging.Handler, level: int) -> logging.Handler:
    """格式 + 上下文 Filter + 归属标记（两种 handler 共用，免得漏挂 Filter 导致 KeyError）。"""
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(_ContextFilter())
    handler.__dict__[HANDLER_MARK] = (
        _KIND_FILE if isinstance(handler, logging.FileHandler) else _KIND_CONSOLE
    )
    return handler

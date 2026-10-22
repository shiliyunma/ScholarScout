"""进程日志落盘：``<home>/run/logs/scout.log``（5MB × 3 轮转 / 密钥脱敏 / 幂等 / 失败降级）。

为什么要有这个模块：打包后的用户说"没反应"时，stderr 是**看不见的**（双击 exe 没有控制台），
于是故障现场等于零。日志必须落在**用户拷得走**的地方：``paths.home()`` 打包后就是 exe 所在
目录，所以日志就在包旁边，用户能把 ``run/logs/scout.log`` 直接发过来。

四条纪律（改这里之前先读）：

1. **文件/目录问题绝不拦住启动**：建目录 / 开文件失败只降级回 stderr，``setup_logging()`` 不抛。
   但 **配置值非法要抛**（见第 4 条）—— 为了记日志让应用起不来是本末倒置，
   而"用户以为改了、其实没改"是另一种更隐蔽的坏。
2. **幂等**：重复调用不重复挂 handler、不重复写行。判据是 handler 上的标记（不是模块级布尔），
   所以 ``scout.py`` / ``__main__`` / 桌面入口各调一次是安全的。
3. **密钥绝不入盘**：:class:`RedactingFilter` 两道（已配置的真实值 + 兜底 ``sk-`` 正则），
   文件和控制台**都挂**（stderr 也可能被重定向进文件）。
4. **配置只有一处**：``runtime.log_level`` / ``log_max_mb`` / ``log_backups``
   由 :mod:`scholarscout.config` 严格校验（非法级别、非正数 → ``ValueError``），
   本模块只负责**用**它们；不在这里另存一份默认值。

渠道子进程不写这里：它有 ``run/channels/<platform>.log``（``channels/supervisor.py`` 把子进程
stderr 重定向过去），混进来会让"渠道日志"与"主进程日志"互相污染。飞书**长连接 CLI**
（``scripts/feishu_ws_up.py``）不是子进程、也没有自己的 stderr 落盘，所以它调本模块。

``session_id`` / ``turn_id`` 用 ``contextvars`` + ``Filter`` 统一补上，**不改任何日志调用点**：
:func:`bind_turn` 在跑轮次的工作线程里挂一次，之后该线程里所有 ``logger.info(...)`` 自带身份。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from scholarscout import paths
from scholarscout.config import (
    DEFAULT_LOG_LEVEL,
    LOG_LEVEL_NAMES,
)

#: 覆盖日志级别的环境变量（``DEBUG`` / ``INFO`` / ``WARNING`` …）；优先于 config.yaml
ENV_LEVEL = "SCS_LOG_LEVEL"

#: 打在自家 handler 上的标记键：幂等判据。**不靠模块级布尔** —— 布尔会被 reload /
#: 多个入口各自 import 的路径绕过，handler 上的标记是跟着 handler 走的。
HANDLER_MARK = "_scs_logging_kind"

#: 脱敏后的占位（不要写 ``***``：看不出"这里被脱敏了"还是"值本来就长这样"）
REDACTED = "[REDACTED]"

#: 行格式：时间 + 级别 + 模块名 + 轮次身份 + 消息。**单行**（不加颜色、不拼多行）
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [sid=%(session_id)s turn=%(turn_id)s]: %(message)s"

#: 没有上下文时的占位。不写 ``None``：看日志的人会以为值就是 "None"
NO_VALUE = "-"

#: 兜底正则（**第二道**）：有前缀的密钥形状。挡的是"没走已知环境变量"的那条路 ——
#: 新来源的 key、手写进 config 的 key、用户粘贴进消息里的 key。
#: 字符集里带 ``-`` / ``_``：真实的 ``sk-proj-xxxx``、``sk-ant-xxxx`` 都在前缀后还有分隔符，
#: 只写 ``[A-Za-z0-9]`` 会在第一个分隔符处断掉 → 整串照样落盘（这坑踩过一次）。
FALLBACK_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{10,}")

#: 环境变量名像密钥的（**第一道**的取值来源之一）：``*_API_KEY`` / ``*_SECRET`` / ``*_TOKEN`` …
SECRET_NAME_PATTERN = re.compile(r"((API|CSE)_?KEY|SECRET|TOKEN|PASSWORD)$", re.IGNORECASE)

#: 明确点名的密钥变量（它们不一定符合上面的后缀规则，或就是本项目的主密钥）
NAMED_SECRET_ENVS = ("LLM_API_KEY", "FEISHU_APP_SECRET", "FEISHU_VERIFY_TOKEN")

#: 只有这么长的值才参与**全局替换**。太短的值（例如 verify token 写成 ``1``）做全局 replace
#: 会把正文里的普通字符全变成 ``[REDACTED]`` —— 脱敏器自己先把日志毁了。
MIN_SECRET_CHARS = 8

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


class RedactingFilter(logging.Filter):
    """把密钥替换成 ``[REDACTED]``。**两道保险，缺一不可**。

    1. **真实值精确替换（主力）**：``secret_values()`` 从环境变量与配置里取到的**非空真实值**，
       逐个 ``str.replace``。为什么必须有这道：飞书 app secret / verify token 长得跟普通随机串
       一模一样，正则根本认不出来 —— 只靠正则会漏。
    2. **兜底正则**：``sk-[A-Za-z0-9_-]{10,}``。挡的是第 1 道没覆盖到的路径（新来源的 key、
       手写进 config 的 key、用户把 key 贴进对话里）。

    构造时传空就不要装（见 :func:`setup_logging`）：没有真实值时空转一道正则毫无意义，
    而"装了却什么都没配"会让人误以为脱敏已经生效。
    """

    def __init__(
        self,
        secrets: Iterable[str] = (),
        *,
        fallback: re.Pattern[str] = FALLBACK_SECRET_PATTERN,
    ) -> None:
        super().__init__()
        # 长的先换：短值是长值前缀时（两个 key 共用前缀），先换短的会把长值切成
        # 「[REDACTED] + 后半截明文」—— 照样漏。
        self._secrets = tuple(sorted({item for item in secrets if item}, key=len, reverse=True))
        self._fallback = fallback

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return self._fallback.sub(REDACTED, text)

    def filter(self, record: logging.LogRecord) -> bool:
        # 先 getMessage()：`logger.info("%s", key)` 这种把密钥放在 args 里的写法，
        # 只改 record.msg 是拦不住的（Formatter 之后才用 args 填 %s）。
        message = record.getMessage()
        redacted = self.redact(message)
        if redacted != message:
            # ``%`` 转义：args 清空后 Formatter 仍会做一次 ``msg % ()``，
            # 正文里本来就有 ``50%`` 时不清零就会 raise（用日志把程序打挂是最坏的失败）。
            record.__dict__["msg"] = redacted.replace("%", "%%")
            record.__dict__["args"] = ()
        if record.exc_info and not record.exc_text:
            # 堆栈里同样可能带密钥（httpx 的 URL、headers 就常带）。只格式化一次并写回，
            # Formatter 看到 exc_text 就不会再拼一遍。
            record.__dict__["exc_text"] = self.redact(
                logging.Formatter().formatException(record.exc_info)
            )
        return True


@dataclass(frozen=True)
class LogOptions:
    """一次 ``setup_logging()`` 解析出来的四个值（级别 / 上限 / 备份 / 待脱敏值）。"""

    level: int
    max_mb: float
    backups: int
    secrets: tuple[str, ...] = ()

    @property
    def max_bytes(self) -> int:
        """``RotatingFileHandler`` 要的字节数（至少 1，免得 0 变成"每条都轮转"）。"""
        return max(1, int(self.max_mb * 1024 * 1024))


def log_path(home: Path | None = None) -> Path:
    """日志文件路径：``<home>/run/logs/scout.log``（路径拼接的唯一入口在 :mod:`paths`）。"""
    return paths.scout_log_path(home)


def secret_values(
    *,
    environ: Mapping[str, str] | None = None,
    extra: Iterable[str] = (),
) -> tuple[str, ...]:
    """要脱敏的**真实密钥值**：环境里点名的 + 名字像密钥的 + 调用方补的（配置里的 key）。

    只留 ``MIN_SECRET_CHARS`` 以上的非空值（理由见该常量）。返回值按长度降序，
    调用方直接用就行。这是纯函数（``environ`` 可注入），所以不碰真实环境也能测。
    """
    env = os.environ if environ is None else environ
    found = {value.strip() for name, value in env.items() if _is_secret_name(name)}
    found.update(str(item).strip() for item in extra)
    return tuple(
        sorted((item for item in found if len(item) >= MIN_SECRET_CHARS), key=len, reverse=True)
    )


def setup_logging(
    *,
    home: Path | None = None,
    level: int | str | None = None,
    max_mb: float | None = None,
    backups: int | None = None,
    stream: IO[str] | None = None,
) -> Path | None:
    """挂上文件 + 控制台 handler（幂等），返回日志文件路径；文件不可用时返回 ``None``。

    参数缺省时按顺序取：显式参数 > ``SCS_LOG_LEVEL``（仅级别）> ``config.yaml`` 的
    ``runtime.log_level`` / ``log_max_mb`` / ``log_backups`` > 内置默认（INFO / 5MB / 3）。

    - ``stream``：控制台输出流；缺省 ``sys.stderr`` —— **控制台输出保留**，行为不变。
    - 文件/目录不可用：**不抛**，降级回 stderr 并说明原因（纪律 1）。
    - 配置值非法：**抛** ``ValueError``（由 :mod:`scholarscout.config` 校验；纪律 4）。
    """
    root = logging.getLogger()
    options = _resolve_options(home, level=level, max_mb=max_mb, backups=backups)
    root.setLevel(options.level)

    file_handler = _own(root, _KIND_FILE)
    if file_handler is None:
        target = _attach_file(root, home, options)
    else:
        _reconfigure(file_handler, options)  # 第二次调只改参数，不再挂一份
        target = _handler_path(file_handler)

    console = _own(root, _KIND_CONSOLE)
    if console is None:
        root.addHandler(_build_handler(logging.StreamHandler(stream or sys.stderr), options))
    else:
        _reconfigure(console, options)
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


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return upper in NAMED_SECRET_ENVS or bool(SECRET_NAME_PATTERN.search(upper))


def _resolve_options(
    home: Path | None,
    *,
    level: int | str | None,
    max_mb: float | None,
    backups: int | None,
) -> LogOptions:
    """四级优先级的解析（详见 :func:`setup_logging` 的 docstring）。"""
    cfg_level, cfg_mb, cfg_backups, cfg_secrets = _read_config_log_settings(home)
    raw_level: object = level if level is not None else (os.environ.get(ENV_LEVEL) or cfg_level)
    return LogOptions(
        level=_level_number(raw_level),
        max_mb=_positive_float(cfg_mb if max_mb is None else max_mb, "runtime.log_max_mb"),
        backups=_positive_int(cfg_backups if backups is None else backups, "runtime.log_backups"),
        secrets=secret_values(extra=cfg_secrets),
    )


def _read_config_log_settings(
    home: Path | None,
) -> tuple[str, float, int, tuple[str, ...]]:
    """读 ``config.yaml`` 的三个日志键 + 配置里的 api key（给脱敏用）。

    **懒 import**：``logging_setup`` 是依赖叶子（``tests/test_layering.py``），
    不该在 import 期把 yaml / 整个 config 拖进来；而这一步本来也只发生在进程启动时一次。

    这里**不吞** ``ValueError``：配置值非法就该在最早的地方炸出来（纪律 4）。
    """
    from scholarscout import config as config_module

    base = home or paths.home()
    cfg = config_module.load_config(base / "config.yaml")
    api_keys = tuple(str(key) for key in (cfg.chat.api_key(), cfg.summary.api_key()) if key)
    return cfg.runtime.log_level, cfg.runtime.log_max_mb, cfg.runtime.log_backups, api_keys


def _level_number(value: object) -> int:
    """级别：接受名字（大小写不敏感）或数字；**认不出来的直接报错**（不静默回 INFO）。"""
    if isinstance(value, bool):
        raise ValueError(f"未知的日志级别：{value!r}")
    if isinstance(value, int):
        return value
    name = str(value if value is not None else DEFAULT_LOG_LEVEL).strip().upper()
    number = logging.getLevelName(name or DEFAULT_LOG_LEVEL)
    if not isinstance(number, int):
        raise ValueError(f"未知的日志级别：{value!r}（允许 {' / '.join(LOG_LEVEL_NAMES)}）")
    return number


def _positive_float(value: object, key: str) -> float:
    """显式参数的另一道校验（正式校验在 config；这里防的是调用方手滑传 0/负数）。"""
    number = float(str(value))
    if number <= 0:
        raise ValueError(f"{key} 必须是正数（收到 {value!r}）")
    return number


def _positive_int(value: object, key: str) -> int:
    """同上，用于备份份数。"""
    number = int(str(value))
    if number <= 0:
        raise ValueError(f"{key} 必须是正整数（收到 {value!r}）")
    return number


def _own(root: logging.Logger, kind: str) -> logging.Handler | None:
    """root 上已有的、本模块挂的那种 handler（幂等的判据）。"""
    for handler in root.handlers:
        if handler.__dict__.get(HANDLER_MARK) == kind:
            return handler
    return None


def _handler_path(handler: logging.Handler) -> Path | None:
    base = handler.__dict__.get("baseFilename")
    return Path(str(base)) if base else None


def _attach_file(root: logging.Logger, home: Path | None, options: LogOptions) -> Path | None:
    """建目录 + 开轮转文件；**任何 IO 失败都只降级**（打一行到 stderr，返回 None）。"""
    target = log_path(home)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            target,
            maxBytes=options.max_bytes,
            backupCount=options.backups,
            encoding="utf-8",
        )
    except (OSError, ValueError) as exc:  # 纪律 1：日志不可用不许拦住启动；原因打到 stderr
        # 用 print 而不是 logger：走到这里说明日志设施本身不可用，它可能也报不出来
        print(
            f"[logging] 日志文件不可用，退回 stderr：{type(exc).__name__}: {exc}", file=sys.stderr
        )
        return None
    _build_handler(handler, options)
    root.addHandler(handler)
    return target


def _build_handler(handler: logging.Handler, options: LogOptions) -> logging.Handler:
    """格式 + 两个 Filter + 归属标记（两种 handler 共用，免得漏挂 Filter 导致 KeyError/漏脱敏）。"""
    handler.setLevel(options.level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.__dict__[HANDLER_MARK] = (
        _KIND_FILE if isinstance(handler, logging.FileHandler) else _KIND_CONSOLE
    )
    _apply_filters(handler, options)
    return handler


def _reconfigure(handler: logging.Handler, options: LogOptions) -> None:
    """重复调用时只改参数：级别可以二次调高/调低，轮转参数跟着最新配置走。"""
    handler.setLevel(options.level)
    if isinstance(handler, logging.handlers.RotatingFileHandler):
        handler.maxBytes = options.max_bytes
        handler.backupCount = options.backups
    _apply_filters(handler, options)


def _apply_filters(handler: logging.Handler, options: LogOptions) -> None:
    """先摘掉本模块自己的 Filter 再挂一遍 —— 否则重复调用会让 Filter 叠成两层。"""
    for existing in [f for f in handler.filters if isinstance(f, _ContextFilter | RedactingFilter)]:
        handler.removeFilter(existing)
    handler.addFilter(_ContextFilter())
    if options.secrets:  # 只有真拿到非空密钥值才装（没有值可换就别假装在脱敏）
        handler.addFilter(RedactingFilter(options.secrets))

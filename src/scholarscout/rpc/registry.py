"""RPC 基础设施：AppContext、方法注册表、参数校验、dispatch。

为什么单独一个文件：methods.py 是 44 个方法的实现文件，机制和业务混在一起，
改一处都得在近千行里翻。这里只有机制，没有业务逻辑。
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from scholarscout.config import Config
from scholarscout.fetch.http import HttpClient
from scholarscout.providers.llm import LlmClient, LlmError
from scholarscout.rpc.errors import Code, RpcError
from scholarscout.runtime import loop
from scholarscout.runtime.session_runtime import DEFAULT_MAX_CONCURRENT, SessionRuntime
from scholarscout.state.db import Database

Handler = Callable[["AppContext", dict[str, Any]], Any]


@dataclass
class AppContext:
    """一次进程运行期的全部依赖。测试里可以塞桩模型与桩出网。"""

    config: Config
    db: Database
    llm: LlmClient | None = None
    http: HttpClient | None = None
    toolsets: Sequence[str] = ("research",)
    emit: Callable[[str, dict[str, Any]], None] | None = None
    cancelled: set[str] = field(default_factory=set)
    #: `session_id -> turn_id`：正在跑的轮次（界面按会话取消时用）
    running: dict[str, str] = field(default_factory=dict)
    #: 多会话并行的工作单元池（docs/20 §2）。**懒建**：CLI / 单会话路径不该被凭空起一个线程池。
    _session_runtime: SessionRuntime | None = field(default=None, init=False, repr=False)
    _runtime_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def require_llm(self) -> LlmClient:
        if self.llm is None:
            raise RpcError(Code.NOT_CONFIGURED, "尚未配置模型客户端（检查 .env 的 LLM_API_KEY）")
        return self.llm

    def session_runtime(self) -> SessionRuntime:
        """拿（必要时创建）多会话工作单元池。

        并发上限读 `runtime.max_parallel_sessions`（**默认 3，用户拍板**）；该配置键属第 8 步，
        这里用 `getattr` 兜默认值 —— 先加能力、不抢后一步的配置契约。
        """
        limit = int(
            getattr(self.config.runtime, "max_parallel_sessions", DEFAULT_MAX_CONCURRENT)
            or DEFAULT_MAX_CONCURRENT
        )
        with self._runtime_lock:
            if self._session_runtime is None:
                self._session_runtime = SessionRuntime(self, max_concurrent=limit)
            return self._session_runtime

    def turn_context(self) -> loop.TurnContext:
        return loop.TurnContext(
            db=self.db,
            config=self.config,
            llm=self.require_llm(),
            http=self.http,
            cancelled=self.cancelled,
            running=self.running,
        )

    def close(self) -> None:
        """关掉会话运行时、出网客户端与数据库（HTTP 连接池不关会留句柄）。

        **顺序要紧**：先 `shutdown()` 会话池（置取消标志 + 把工作线程摘出退出 join 名单），
        否则关窗时进程会一直卡到正在跑的那一轮结束。

        **还有轮次在跑时不关共享资源**（早退）：`shutdown()` 不 join，工作线程可能正处在
        一轮的中间（落消息、抓网页），此时从主线程 close 掉 SQLite 连接或 HTTP 连接，
        就是**跨线程 use-after-close** —— 本机实测是**进程级 access violation**（0xC0000005，
        子进程驱动用例抓的，见 `tests/runtime/test_session_runtime.py::_DRIVER`）。
        进程本来就要退了：句柄交给操作系统回收；WAL 下每次写都已提交，库不会因此损坏。
        """
        runtime = self._session_runtime
        if runtime is not None:
            runtime.shutdown()
            if runtime.busy:
                return
        if self.http is not None:
            self.http.close()
        self.db.close()


_METHODS: dict[str, Handler] = {}


def method(name: str) -> Callable[[Handler], Handler]:
    def decorator(func: Handler) -> Handler:
        if name in _METHODS:
            raise ValueError(f"RPC 方法重复注册：{name}")
        _METHODS[name] = func
        return func

    return decorator


def method_names() -> list[str]:
    """已注册的方法名（桌面壳的资源校验拿它比对前端调用，见 docs/01 §3）。"""
    return sorted(_METHODS)


def _params(params: dict[str, Any] | None) -> dict[str, Any]:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise RpcError(Code.INVALID_PARAMS, "params 必须是对象")
    return params


def _require(params: dict[str, Any], key: str, kind: type | tuple[type, ...] = str) -> Any:
    if key not in params:
        raise RpcError(Code.INVALID_PARAMS, f"缺参数：{key}")
    value = params[key]
    if not isinstance(value, kind):
        raise RpcError(Code.INVALID_PARAMS, f"参数 {key} 类型不对：期望 {kind}")
    return value


def _int_param(params: dict[str, Any], key: str, default: int) -> int:
    value = params.get(key, default)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise RpcError(Code.INVALID_PARAMS, f"参数 {key} 必须是数字")
    return int(value)


# --------------------------------------------------------------------- 系统


def dispatch(ctx: AppContext, name: str, params: dict[str, Any] | None = None) -> Any:
    handler = _METHODS.get(name)
    if handler is None:
        raise RpcError(Code.METHOD_NOT_FOUND, f"未知方法：{name}")
    return handler(ctx, _params(params))


def llm_ok(ctx: AppContext) -> bool:
    """给前端做能力探测用；不抛异常。"""
    try:
        ctx.require_llm()
        return True
    except (RpcError, LlmError):
        return False


def clamp_limit(value: Any, default: int, maximum: int = 500) -> int:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return default
    return max(1, min(int(value), maximum))


def is_finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)

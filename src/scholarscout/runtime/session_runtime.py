"""多会话并行：每个会话一个工作单元 + 上限 ``max_workers`` 的线程池（docs/20 §2/§3）。

**这一层只做调度，不做对话** —— ``runtime/loop.run_turn`` 的内部逻辑一字未改（docs/20 §2.2）：
并行是"加一层调度"，不是重写内核。

为什么需要它（用户原话）：「在其中一个会话运行时，我想切换到其他会话，会导致本次会话出现异常」。
根因之一是**后端只有"全局单飞 + 单一进行中槽"**：进行中登记与取消信号都是进程内一份、按引用共用。
这里把两者搬进各自的会话单元。

四条硬约束（违反任何一条都不算达成）：

1. **一个会话同时只能有一轮在跑**（``turn_in_progress``）：``messages.idx`` 是会话内自增且
   ``UNIQUE(session_id, idx)``、``research_states`` 是一会话一行 —— 同一会话两轮并行不是"并行"，
   是数据损坏（docs/20 §2.3）；
2. **每轮一个独立的 ``threading.Event`` 取消信号 + 每会话自己的进行中登记**：
   "取消 A 误伤 B" 从机制上不可能（docs/20 §6.2）；
3. **上限 3（用户拍板）、池满不排队**：第 4 个 ``submit_turn`` 直接回 ``at_capacity`` + 谁在跑。
   容量闸在我们自己手里，**绝不把任务丢进 executor 的队列** —— 否则"排队"会以"看不见的等待"
   的形式偷偷回来（用户明确不要排队，docs/20 §5.3）；
4. **关窗进程必须立刻能退**：``shutdown()`` 置取消标志、不 join，并把工作线程摘出解释器退出时的
   join 名单（理由与实测数字见 :func:`detach_threads_from_exit_join`）。
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol

from scholarscout.config import DEFAULT_MAX_PARALLEL_SESSIONS, Config
from scholarscout.fetch.http import HttpClient
from scholarscout.providers.llm import LlmClient
from scholarscout.runtime import budget as daily_budget
from scholarscout.runtime import loop, notify
from scholarscout.runtime.loop import Emitter, TurnResult
from scholarscout.state import sessions
from scholarscout.state.db import Database

logger = logging.getLogger(__name__)

#: 默认并发上限（用户拍板，docs/20 §5.1）。**唯一来源是 config 层的常量** ——
#: `runtime.max_parallel_sessions` 的默认值与这里的兜底必须是同一个数，否则"没配时 3、配了才变"会漂。
DEFAULT_MAX_CONCURRENT = DEFAULT_MAX_PARALLEL_SESSIONS
#: 空闲单元回收阈值（拍脑袋值，需按真机内存占用调 —— docs/20 §10 #6 已如实标注）
DEFAULT_IDLE_REAP_S = 900.0
#: 结果信箱保留条数（供 `chat.result` 补取；太旧的一轮不再接受补取，如实回 `unknown_turn`）
DEFAULT_KEEP_RESULTS = 32
#: 工作线程名前缀：真机排查（卡住/超时）时 `threading.enumerate()` 一眼能认出是谁
THREAD_PREFIX = "scout-turn"

#: 事件 → 阶段（给 `chat.status` 与界面角标用）。未知事件不改阶段 —— 猜阶段只会误导。
_PHASE_BY_EVENT = {
    "chat.delta": "writing",
    "chat.tool_call": "tool",
    "chat.tool_result": "thinking",
}


class RuntimeHost(Protocol):
    """工作单元池对宿主的**最小**要求（结构化类型）。

    为什么用 Protocol 而不是直接 import `rpc.registry.AppContext`：分层门禁
    （`tests/test_layering.py`）不允许 `runtime/` 依赖 `rpc/`，**连 TYPE_CHECKING 里 import 也不行**
    （门禁用 AST 扫全部 ImportFrom 节点）。结构化类型让依赖方向保持"rpc → runtime"。
    """

    db: Database
    config: Config
    http: HttpClient | None

    def require_llm(self) -> LlmClient: ...


@dataclass
class TurnHandle:
    """一轮的句柄：**只属于这一轮**（取消信号是它自己的 Event，不是全局集合）。"""

    session_id: str
    turn_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    #: 轮次收尾时置位：同步模式（`chat.send` 默认 `mode="sync"`）靠它等这一轮
    done_event: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.monotonic)
    phase: str = "queued"
    #: 提交这一轮时**界面上正在显示**哪个会话（由 `chat.send` 的 `active_session_id` 传进来）。
    #: 跑完提醒据此判"要不要提醒" —— 运行时自己猜"当前会话"在并发下必然猜错（docs/20 §6.3）。
    active_session_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "phase": self.phase,
            "started_at": self.started_at,
        }


@dataclass
class SessionUnit:
    """一个会话的全部**活动**状态（逐项对应 docs/20 §2.2 的核对清单）。

    **不与任何别的会话共用可变对象**：
    - `running`：这个会话此刻在跑哪一轮（会话内登记，不是全局 dict）；
    - `cancelled`：这个会话的按-turn_id 取消留痕（不是全局 set）；
    - `handle` / `result` / 用量账：本会话自己的。

    库里才是真源，单元只是**热缓存**（销毁不丢任何持久数据，docs/20 §2.3 第 4 态）。
    """

    session_id: str
    running: dict[str, str] = field(default_factory=dict)
    cancelled: set[str] = field(default_factory=set)
    turns: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    last_turn_id: str | None = None
    last_stop_reason: str | None = None
    handle: TurnHandle | None = None
    result: TurnResult | None = None
    last_active: float = field(default_factory=time.monotonic)

    def usage(self) -> dict[str, Any]:
        """会话累计用量（token / 工具调用次数）—— 第 8 步封顶与 `chat.status` 都读它。"""
        return {
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "tokens": {"prompt": self.prompt_tokens, "completion": self.completion_tokens},
            "last_turn_id": self.last_turn_id,
            "last_stop_reason": self.last_stop_reason,
        }


def detach_threads_from_exit_join(thread_prefix: str) -> int:
    """把本池的工作线程从**解释器退出时的 join 名单**里摘出来（返回摘掉的个数）。

    为什么必须做（本机实测，不是理论）：`ThreadPoolExecutor` 的工作线程是**非 daemon**，
    解释器退出时 `concurrent.futures.thread._python_exit` 会逐个 `join`，
    `threading._shutdown` 又会 join 所有非 daemon 线程 —— **即使已经 `shutdown(wait=False)`**。
    而一轮对话要跑 3–12 分钟，于是"关窗"会一直卡到那一轮跑完。实测（一个卡在 Event 上的任务）：

    | 处理 | 进程实际退出耗时 |
    |---|---|
    | 什么都不做（`shutdown(wait=False)`） | **20.2s**（任务卡多久就卡多久） |
    | 只摘 `_threads_queues` | 20.2s（`threading._shutdown` 仍在 join） |
    | 再摘 `threading._shutdown_locks` | **0.54s** |

    `thread.daemon = True` 帮不上忙：CPython 对**已启动**的线程设 daemon 直接抛
    `RuntimeError: cannot set daemon status of active thread`（实测），所以只能摘注册表。
    这是本模块唯一碰私有 API 的地方，全部包在 try/except 里且**可测**：万一 CPython 改了实现，
    最坏退回"退出时会 join"（功能不受影响），而 `tests/runtime/test_session_runtime.py`
    里既断言了机制、又用子进程实测了退出耗时，静默退化会被抓住。
    """

    targets = [thread for thread in threading.enumerate() if thread.name.startswith(thread_prefix)]
    if not targets:
        return 0
    try:
        from concurrent.futures import thread as futures_thread
    except Exception:  # pragma: no cover - 标准库结构变了
        return 0

    shutdown_locks = getattr(threading, "_shutdown_locks", None)
    lock = getattr(threading, "_shutdown_locks_lock", None)
    queue_registry = getattr(futures_thread, "_threads_queues", None)
    queue_lock = getattr(futures_thread, "_global_shutdown_lock", None)

    acquired: list[Any] = []
    for candidate in (lock, queue_lock):
        if candidate is not None:
            try:
                candidate.acquire()
                acquired.append(candidate)
            except Exception as exc:  # pragma: no cover - 拿不到锁就退回"会 join"
                # 仍然吞掉（拿不到锁 = 最坏退回"退出时会 join"，功能不受影响），
                # 但记一行：私有锁拿不到说明 CPython 内部结构变了，静默等于下次才发现退出变慢。
                logger.warning(
                    "摘除守护线程：拿不到 %r 的锁，退回退出时 join：err=%s", candidate, exc
                )
    try:
        for thread in targets:
            tstate = getattr(thread, "_tstate_lock", None)
            if shutdown_locks is not None and tstate is not None:
                with contextlib.suppress(Exception):
                    shutdown_locks.discard(tstate)
            # 鸭子类型而不是 `isinstance(queue_registry, dict)`：标准库里这里是
            # `weakref.WeakKeyDictionary`（**不是 dict 的子类**），按 dict 判会静默不摘 ——
            # 那样 `concurrent.futures.thread._python_exit` 照旧 join 工作线程，关窗照样卡住。
            # （这个坑是本模块的测试用子进程实测退出耗时抓出来的，不是靠读注释。）
            pop = getattr(queue_registry, "pop", None)
            if callable(pop):
                with contextlib.suppress(Exception):
                    pop(thread, None)
    finally:
        for candidate in reversed(acquired):
            with contextlib.suppress(Exception):
                candidate.release()
    return len(targets)


class SessionRuntime:
    """每个会话一个工作单元；上限 `max_concurrent` 的线程池执行；**读循环永不阻塞**。"""

    def __init__(
        self,
        host: RuntimeHost,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        idle_reap_s: float = DEFAULT_IDLE_REAP_S,
        keep_results: int = DEFAULT_KEEP_RESULTS,
        thread_prefix: str = THREAD_PREFIX,
        notifier: notify.TurnDoneNotifier | None = None,
    ) -> None:
        self._host = host
        self._max_concurrent = max(1, int(max_concurrent))
        self._idle_reap_s = float(idle_reap_s)
        self._keep_results = max(1, int(keep_results))
        self._thread_prefix = thread_prefix
        #: 跑完提醒（docs/20 §6.3）。**显式传 None 时按配置造**：`notify_turn_done=false`
        #: （默认）→ 造出来的是 `None`，`_notify` 整段零成本、一次外网都不发。
        self._notifier = (
            notifier
            if notifier is not None
            else notify.from_config(host.config.runtime, db=host.db, http=host.http)
        )
        self._lock = threading.RLock()
        self._units: dict[str, SessionUnit] = {}
        self._handles: dict[str, TurnHandle] = {}
        self._results: OrderedDict[str, TurnResult] = OrderedDict()
        #: 正在跑的轮次数（容量闸的真值；与 `_units` 里有无 handle 一致，但这里是 O(1) 且抗竞态）
        self._in_flight = 0
        self._closed = False
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_concurrent, thread_name_prefix=self._thread_prefix
        )

    # --------------------------------------------------------------- 提交

    def submit_turn(
        self,
        *,
        session_id: str,
        text: str,
        toolsets: Sequence[str],
        emit: Emitter | None,
        before_start: Callable[[], None] | None = None,
        active_session_id: str | None = None,
    ) -> dict[str, Any]:
        """提交一轮，**立即返回**（不等结果）。

        - 成功：``{"ok": True, "turn_id": ...}`` —— `turn_id` 是**预分配**的，所以 RPC 层能在
          整轮结束前一秒就把它交给界面（docs/20 §3.2 的 `mode="async"`）。
        - 拒绝：``{"ok": False, "reason": "turn_in_progress"|"at_capacity"|"shutdown"|"daily_budget_exceeded", "running": [...]}``。
          **拒绝而不是排队**（用户明确不要排队，docs/20 §5.3）。

        ``before_start`` 在**工作线程里、跑轮次之前**执行：给"会调模型的前置步骤"（如标题总结）用 ——
        放在这里才不会让 `mode="async"` 的 RPC 调用被它拖住（阶段 4 的验收是 <10ms 返回 turn_id）。

        ``active_session_id`` 是**提交这一刻界面上正在显示的会话**（跑完提醒要用它判"要不要提醒"）。
        运行时不猜：缺省即"界面没在看任何会话"，于是提醒一条都不发（``no_active_session``）。
        """
        # ---- 每日封顶的第一处检查点（docs/20 §5.2）：已超则**拒绝新轮次**。
        # 没配上限时 `guard is None` → 不查库，行为与引入前逐字一致。
        guard = daily_budget.from_runtime(self._host.config.runtime)
        if guard is not None:
            exceeded = guard.exceeded(self._host.db)
            if exceeded is not None:
                return {
                    "ok": False,
                    "reason": daily_budget.REJECT_REASON,
                    "budget": exceeded,
                    "running": self._running_snapshot(),
                }
        with self._lock:
            if self._closed:
                return {"ok": False, "reason": "shutdown", "note": "会话运行时已关闭"}
            unit = self._units.get(session_id)
            if unit is None:
                unit = SessionUnit(session_id=session_id)
                self._units[session_id] = unit
            if unit.handle is not None:
                return {
                    "ok": False,
                    "reason": "turn_in_progress",
                    "turn_id": unit.handle.turn_id,
                    "running": self._running_locked(),
                }
            if self._in_flight >= self._max_concurrent:
                return {"ok": False, "reason": "at_capacity", "running": self._running_locked()}

            handle = TurnHandle(
                session_id=session_id,
                turn_id=sessions.new_id("trn"),
                active_session_id=(str(active_session_id or "").strip() or None),
            )
            unit.handle = handle
            unit.last_active = time.monotonic()
            self._handles[handle.turn_id] = handle
            self._in_flight += 1
            future = self._executor.submit(
                self._run_turn, unit, handle, text, list(toolsets), emit, before_start
            )
            # 兜底：即使 `_finish` 本身出意外，同步等待的那一侧也必须被放开（不能挂死一个 RPC 线程）
            future.add_done_callback(lambda _future: handle.done_event.set())
        return {"ok": True, "turn_id": handle.turn_id}

    def wait(self, turn_id: str, *, timeout: float | None = None) -> TurnResult | None:
        """等一轮跑完并返回结论（**同步语义**用；`timeout=None` = 一直等）。

        为什么不是 `future.result()`：那样会把"轮次里抛了什么"重新抛到调用线程上，
        而本设计的口径是**异常一律收敛成 `stop_reason`**、不外抛（docs/20 §6.1）。
        """
        with self._lock:
            handle = self._handles.get(turn_id)
        if handle is None:
            return self.result(turn_id)
        if not handle.done_event.wait(timeout):
            return None
        return self.result(turn_id)

    # --------------------------------------------------------------- 取消

    def cancel(
        self, *, session_id: str | None = None, turn_id: str | None = None
    ) -> dict[str, Any]:
        """请求取消某一轮：**只影响被点到的那一个会话**（每轮一个 Event，docs/20 §6.2）。

        执行时机是**步间检查**：正在跑的那一步（模型调用或工具调用）结束后才生效。
        """
        with self._lock:
            handle: TurnHandle | None = None
            unit: SessionUnit | None = None
            if turn_id:
                handle = self._handles.get(turn_id)
                unit = self._units.get(handle.session_id) if handle is not None else None
            elif session_id:
                unit = self._units.get(session_id)
                handle = unit.handle if unit is not None else None
            if handle is None or unit is None:
                return {
                    "ok": False,
                    "reason": "no_running_turn",
                    "note": "这个会话当前没有正在跑的轮次（可能已经结束）",
                }
            unit.cancelled.add(handle.turn_id)
            handle.cancel_event.set()
            return {
                "ok": True,
                "turn_id": handle.turn_id,
                "note": "已请求取消：当前这一步结束后生效（步间检查）",
            }

    # --------------------------------------------------------------- 观测

    def status(self, session_id: str | None = None) -> dict[str, Any]:
        """哪些会话在跑 / 跑在什么阶段 / 容量还剩多少 / 今日用量（docs/20 §3.2 的 `chat.status`）。

        ``global_budget``：滚动 24 小时的已用/上限/是否超限（`runtime/budget.py` 的稳定形状）。
        **没配上限时** ``enabled=False``、各项为 ``None`` —— 不假报 0（0 会被读成"没花钱"）。
        """
        # 预算快照要在锁外取（要查库，不该占着运行时锁）
        global_budget = daily_budget.snapshot(self._host.config.runtime, self._host.db)
        with self._lock:
            running = [
                unit.handle.as_dict() for unit in self._units.values() if unit.handle is not None
            ]
            if session_id:
                running = [item for item in running if item["session_id"] == session_id]
            payload: dict[str, Any] = {
                "running": running,
                "capacity": {"max": self._max_concurrent, "used": self._in_flight},
                # 滚动 24 小时的用量/上限/是否超限（`budget.snapshot` 的稳定形状）。
                # 没配上限时 `enabled=False` 且各项为 None —— 不假报 0。
                "global_budget": global_budget,
            }
            if session_id:
                unit = self._units.get(session_id)
                payload["session"] = {
                    "session_id": session_id,
                    "running": bool(unit is not None and unit.handle is not None),
                    # 热缓存；**权威源是 usage_records**（`session.usage` 的取数口径）
                    "usage": unit.usage() if unit is not None else None,
                }
            else:
                payload["sessions"] = [
                    {
                        "session_id": unit.session_id,
                        "running": unit.handle is not None,
                        **unit.usage(),
                    }
                    for unit in self._units.values()
                ]
            return payload

    def result(self, turn_id: str) -> TurnResult | None:
        """最近 `keep_results` 轮的结果信箱（迟到 / 掉线重连补取）。"""
        with self._lock:
            return self._results.get(turn_id)

    # --------------------------------------------------------------- 生命周期

    def reap_idle(self, *, idle_s: float | None = None) -> int:
        """释放空闲单元（默认 15 分钟无活动）—— **库里才是真源**，销毁不丢任何持久数据。"""
        threshold = self._idle_reap_s if idle_s is None else float(idle_s)
        now = time.monotonic()
        with self._lock:
            victims = [
                session_id
                for session_id, unit in self._units.items()
                if unit.handle is None and now - unit.last_active >= threshold
            ]
            for session_id in victims:
                del self._units[session_id]
            return len(victims)

    def shutdown(self) -> None:
        """置取消标志、**不 join**（与 `serve()` 的 daemon 语义一致，docs/20 §2.3）。

        关窗时调用：正在跑的那一轮会在下一个步间边界收口；进程**立刻**能退
        （线程摘出解释器退出 join 名单，理由见 :func:`detach_threads_from_exit_join`）。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handles = [unit.handle for unit in self._units.values() if unit.handle is not None]
        for handle in handles:
            handle.cancel_event.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
        detach_threads_from_exit_join(self._thread_prefix)

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def busy(self) -> bool:
        """还有轮次在跑吗（关窗时判"能不能安全关掉共享资源"用，见 `AppContext.close()`）。"""
        with self._lock:
            return self._in_flight > 0

    @property
    def closed(self) -> bool:
        return self._closed

    # --------------------------------------------------------------- 内部

    def _running_locked(self) -> list[dict[str, Any]]:
        return [unit.handle.as_dict() for unit in self._units.values() if unit.handle is not None]

    def _running_snapshot(self) -> list[dict[str, Any]]:
        """加锁版的"谁在跑"（预算拒绝是在锁外判的，但载荷仍要如实带上运行清单）。"""
        with self._lock:
            return self._running_locked()

    def _turn_context(self, unit: SessionUnit, handle: TurnHandle) -> loop.TurnContext:
        """这一轮的上下文：取消信号与进行中登记**都取自本单元**（不跨会话共用）。"""
        return loop.TurnContext(
            db=self._host.db,
            config=self._host.config,
            llm=self._host.require_llm(),
            http=self._host.http,
            running=unit.running,
            cancelled=unit.cancelled,
            cancel_event=handle.cancel_event,
        )

    def _phase_emitter(self, handle: TurnHandle, emit: Emitter | None) -> Emitter | None:
        """按事件更新 `phase`（`chat.status` / 界面角标要它），其余原样透传。"""
        if emit is None:
            return None

        def on_event(kind: str, payload: dict[str, Any]) -> None:
            phase = _PHASE_BY_EVENT.get(kind)
            if phase is not None and payload.get("turn_id") == handle.turn_id:
                handle.phase = phase
            emit(kind, payload)

        return on_event

    def _run_turn(
        self,
        unit: SessionUnit,
        handle: TurnHandle,
        text: str,
        toolsets: list[str],
        emit: Emitter | None,
        before_start: Callable[[], None] | None,
    ) -> None:
        """工作线程里的一轮：**异常一律收敛**，只写本会话的事件（docs/20 §6.1）。"""
        bound = loop.bind_emitter(emit, unit.session_id)
        result: TurnResult | None = None
        try:
            if before_start is not None:
                before_start()
            handle.phase = "thinking"
            result = loop.run_turn(
                self._turn_context(unit, handle),
                session_id=unit.session_id,
                text=text,
                toolsets=toolsets,
                emit=self._phase_emitter(handle, bound),
                turn_id=handle.turn_id,
            )
        except BaseException as exc:  # 兜住 loop 之外的意外（如包装 emitter 本身出错）
            result = self._error_result(unit, handle, exc, bound)
        finally:
            self._finish(unit, handle, result)

    def _error_result(
        self, unit: SessionUnit, handle: TurnHandle, exc: BaseException, emit: Emitter | None
    ) -> TurnResult:
        """把"轮次之外"的意外变成一轮正常收尾的结果：**只写本会话的事件**，不污染别的会话。"""
        message = f"{type(exc).__name__}: {exc}"
        if emit is not None:
            emit("chat.error", {"turn_id": handle.turn_id, "code": -32603, "message": message})
            emit(
                "chat.done",
                {
                    "turn_id": handle.turn_id,
                    "stop_reason": "error",
                    "usage": {},
                    "refs": {},
                    "text": "",
                    "message_id": "",
                    "error": message,
                },
            )
        return TurnResult(
            turn_id=handle.turn_id,
            session_id=unit.session_id,
            message_id="",
            text="",
            stop_reason="error",
            error=message,
        )

    def _finish(self, unit: SessionUnit, handle: TurnHandle, result: TurnResult | None) -> None:
        with self._lock:
            if unit.handle is handle:
                unit.handle = None
            unit.result = result
            if result is not None:
                unit.turns += 1
                unit.tool_calls += len(result.tool_calls or [])
                unit.prompt_tokens += int(result.usage.get("prompt_tokens") or 0)
                unit.completion_tokens += int(result.usage.get("completion_tokens") or 0)
                unit.last_turn_id = result.turn_id
                unit.last_stop_reason = result.stop_reason
                self._results[result.turn_id] = result
                while len(self._results) > self._keep_results:
                    self._results.popitem(last=False)
            unit.last_active = time.monotonic()
            self._handles.pop(handle.turn_id, None)
            self._in_flight = max(0, self._in_flight - 1)
            handle.phase = "done"
        handle.done_event.set()
        # 外部提醒（docs/20 §6.3 第 (2) 条）放在 `done_event` **之后**：等结果的调用方（`mode="sync"`）
        # 先拿到结论 —— 一次私聊推送（可能要先取 tenant token）不该拖慢"这一轮已经结束了"这件事。
        # 开关关着时 `self._notifier is None`，这里零成本返回、一次外网都不发。
        self._notify_done(unit, handle, result)

    def _notify_done(
        self, unit: SessionUnit, handle: TurnHandle, result: TurnResult | None
    ) -> None:
        """轮次收尾 → 非活跃会话 → 私聊一条摘要。**任何失败只记日志，绝不影响轮次结论。**"""
        notifier = self._notifier
        if notifier is None or result is None:
            return
        try:
            row = sessions.get_session(self._host.db, unit.session_id)
            title = str((row or {}).get("title") or "")
            notifier.notify(
                turn_id=handle.turn_id,
                session_id=unit.session_id,
                title=title,
                text=result.text,
                stop_reason=result.stop_reason,
                duration_s=time.monotonic() - handle.started_at,
                # 活跃会话**只认提交那一刻界面报上来的那个**：运行时自己猜在并发下必然猜错
                active_session_id=handle.active_session_id,
            )
        except Exception as exc:  # 提醒失败不该把一轮已经成功的对话变成失败
            logger.warning("[Notify] 跑完提醒收尾异常（已吞）：turn=%s err=%s", handle.turn_id, exc)

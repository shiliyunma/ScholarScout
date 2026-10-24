"""stdio 传输：一行一个 JSON-RPC 消息（UTF-8，无 BOM）。

设计：**分发逻辑只有一份**（rpc/methods.py），这里只负责"读一行 → 分发 → 写回/上报通知"。
桌面壳（pywebview）走同一份方法表，只是传输不同（见 docs/01 §2 的说明）。
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable, Iterable
from typing import Any, TextIO

from scholarscout.rpc.errors import Code, RpcError, from_exception
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.stdio import force_utf8_stream

Emitter = Callable[[str, dict[str, Any]], None]

#: 作为通知上报的事件前缀（docs/01 §4）
NOTIFICATION_METHODS = (
    "chat.delta",
    "chat.tool_call",
    "chat.tool_result",
    "chat.done",
    "chat.error",
    # 子 Agent P1（docs/16 §3.3）：顺序子任务的生命周期事件（**不动 chat.delta**）
    "chat.subtask_start",
    "chat.subtask_done",
    # 方案 3（检索偏好位）：分类置信度低时不搜，把"该选哪类资料"推给界面渲染选择卡。
    # **漏登记会被加 chat. 前缀**（chat.chat.needs_choice），界面永远收不到。
    "chat.needs_choice",
    # py.exec 审批（docs/24 §5.1）：`sandbox.approve` 的入口就是这张卡，卡片要完整代码 +
    # pending 清单 + 四个选项（`tools/py_exec.py::_approval_event`）。
    # **漏登记会被加 chat. 前缀**（chat.chat.needs_approval），界面永远收不到。
    "chat.needs_approval",
    # 抽背「不会」的流式增量（docs/01 §4）：正文风格与 chat.delta 不同 —— 它是**增量片段**、
    # 前端追加显示，所以单列一个名字（漏登记会被加上 chat. 前缀、界面永远收不到）。
    "practice.delta",
    # 会话标题在轮次开始前就定下来（用户一发送就能看到名字）
    "session.updated",
    # 渠道中心（docs/21 §5.1）：渠道子进程的起停/连接状态变化 → 界面状态点随它变
    "channel.state",
)


def make_writer(stream: TextIO) -> Callable[[dict[str, Any]], None]:
    def write(payload: dict[str, Any]) -> None:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stream.flush()

    return write


def make_emitter(write: Callable[[dict[str, Any]], None]) -> Emitter:
    """把 loop 的事件回调变成 JSON-RPC 通知。"""

    def emit(kind: str, payload: dict[str, Any]) -> None:
        method = kind if kind in NOTIFICATION_METHODS else f"chat.{kind}"
        write({"jsonrpc": "2.0", "method": method, "params": payload})

    return emit


def error_response(request_id: Any, error: RpcError) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": error.to_error()}


def ok_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def handle_message(
    ctx: AppContext,
    message: dict[str, Any],
    *,
    dispatcher: Callable[[AppContext, str, Any], Any] = dispatch,
) -> dict[str, Any] | None:
    """处理一条已解析的消息；通知（无 id）返回 None。"""
    request_id = message.get("id")
    name = message.get("method")
    if not isinstance(name, str):
        return error_response(request_id, RpcError(Code.INVALID_REQUEST, "缺少 method"))

    # 通知：没有 id 就不回响应（当前没有客户端→服务端通知，先按契约实现）
    if request_id is None:
        try:
            dispatcher(ctx, name, message.get("params"))
        except RpcError:
            return None
        return None

    try:
        result = dispatcher(ctx, name, message.get("params"))
    except BaseException as exc:
        return error_response(request_id, from_exception(exc))
    return ok_response(request_id, result)


def handle_line(
    ctx: AppContext,
    line: str,
    *,
    dispatcher: Callable[[AppContext, str, Any], Any] = dispatch,
) -> dict[str, Any] | None:
    text = line.strip()
    if not text:
        return None
    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        return error_response(None, RpcError(Code.PARSE_ERROR, f"JSON 解析失败：{exc}"))
    if not isinstance(message, dict):
        return error_response(None, RpcError(Code.INVALID_REQUEST, "消息必须是 JSON 对象"))
    return handle_message(ctx, message, dispatcher=dispatcher)


#: 同时在跑的请求上限（一轮对话占 1 个；其余留给界面点开资料/取消/设置）
MAX_CONCURRENT_REQUESTS = 4


def serve(
    ctx: AppContext,
    *,
    stdin: Iterable[str] | None = None,
    stdout: TextIO | None = None,
    dispatcher: Callable[[AppContext, str, Any], Any] = dispatch,
    max_workers: int = MAX_CONCURRENT_REQUESTS,
) -> int:
    """**并发**服务：读一行 → 交给工作线程分发 → 写回，直到 stdin 关闭。

    为什么必须并发（真机实测的故障）：单线程时一轮 `chat.send` 会阻塞 3–12 分钟，
    期间界面点资料库预览（要 `archive.*`）、点「停止」（`chat.cancel`）全都排在它后面 ——
    用户看到的是"点了没反应"，而取消永远不会生效。JSON-RPC 有 `id`，
    响应本来就允许乱序，所以并发是安全且必需的。

    并发安全的前提（已分别落实）：
    - 写 stdout 加锁：一行一条响应的完整性靠锁，不靠时序；
    - 数据库 WAL + 每语句 autocommit（见 `state/db.py`）→ 一写多读；
    - 工作线程是 daemon：关窗（stdin 关闭）时立刻能退出，不会等一轮跑完。
    """
    input_stream = stdin if stdin is not None else sys.stdin
    output = stdout if stdout is not None else sys.stdout
    # 帧读写统一 UTF-8（`rpc/server.py` 的传输声明）：入口已经重设过标准流，
    # 这里再对**传进来的**流补一次 —— 发布包里 skip 任何一个入口都不会退回 cp936。
    force_utf8_stream(input_stream)
    force_utf8_stream(output)
    raw_write = make_writer(output)
    write_lock = threading.Lock()

    def write(payload: dict[str, Any]) -> None:
        with write_lock:
            raw_write(payload)

    ctx.emit = make_emitter(write) if ctx.emit is None else ctx.emit
    slots = threading.Semaphore(max(1, max_workers))

    def run_task(line: str) -> None:
        try:
            response = handle_line(ctx, line, dispatcher=dispatcher)
            if response is not None:
                write(response)
        finally:
            slots.release()

    try:
        for line in input_stream:
            slots.acquire()
            threading.Thread(
                target=run_task, args=(line,), daemon=True, name="scout-rpc-worker"
            ).start()
    except KeyboardInterrupt:  # pragma: no cover - 手工调试时的退出路径
        return 0
    return 0

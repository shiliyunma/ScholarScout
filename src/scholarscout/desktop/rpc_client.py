"""桌面壳与后端之间的唯一通道：**子进程 + 一行一个 JSON-RPC**。

设计取舍：壳里不放任何业务逻辑，只做三件事 —— 起进程、转发请求、把通知推给 JS。
这样"后端能力"与"界面"不会各长一套（docs/01 §2）。

传输细节：
- 每个请求一个自增 ``id``，**串行化**（一把锁），避免响应串台
- 没有 ``id`` 的行是通知（``chat.delta`` / ``chat.tool_call`` / …）→ 交给 ``on_event``
- 解析不了的行直接忽略（后端若被第三方库的日志污染，不该把界面搞崩）
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

EventSink = Callable[[str, dict[str, Any]], None]

#: 读到输出结束的哨兵
_EOF = object()
#: 默认等待上限（秒）：排学习路线要调模型，给足时间
DEFAULT_TIMEOUT = 180.0


class RpcCallError(RuntimeError):
    """调用失败：传输层问题或后端返回的 error。"""

    def __init__(self, message: str, *, code: int = -32000) -> None:
        super().__init__(message)
        self.code = code


class ProcessLike(Protocol):
    """只要满足这个形状，就能当后端进程用（测试里塞假进程）。"""

    stdin: Any
    stdout: Any

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


def default_env() -> dict[str, str]:
    """子进程环境：强制 UTF-8；**源码运行时**再把 ``src/`` 挂进 PYTHONPATH。

    打包后**不许**挂 PYTHONPATH（踩过）：打包目录里同样有一份 `scholarscout/` 源码，
    一旦把它挂进 PYTHONPATH，子进程会去 import **源码**而不是打进包的模块 ——
    行为与包不一致，而且那条路径下的输出编码会退回 GBK，壳按 UTF-8 读就 `UnicodeDecodeError`
    → 壳判定"后端进程已退出"。真机症状：界面能开，页头红字"连接后端失败"。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if getattr(sys, "frozen", False):
        return env
    src = Path(__file__).resolve().parents[2]
    if not src.is_dir():
        return env
    existing = env.get("PYTHONPATH", "")
    parts = [str(src)] + ([existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def default_command(*, db_path: str | Path | None = None, toolsets: str = "research") -> list[str]:
    """后端命令：与手工跑 ``--rpc`` 完全一致，壳不搞特殊通道。

    **打包后要拉起自己**（`sys.executable --rpc`），不能再拼 `-m scholarscout`：
    PyInstaller 的引导器不认 `-m 模块`，那样起出来的子进程会立刻退出 ——
    真机表现是界面能开、页头却红字写着「连接后端失败：后端进程已退出」。
    """
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--rpc", "--toolsets", toolsets]
    else:
        command = [sys.executable, "-m", "scholarscout", "--rpc", "--toolsets", toolsets]
    if db_path is not None:
        command += ["--db", str(db_path)]
    return command


def _result_of(message: dict[str, Any]) -> Any:
    """把一条响应消息变成结果（或抛出后端错误）。"""
    error = message.get("error")
    if isinstance(error, dict):
        raise RpcCallError(
            str(error.get("message") or "后端返回错误"),
            code=int(error.get("code") or -32000),
        )
    return message.get("result")


class RpcClient:
    """一行一 JSON 的同步客户端（后台线程收行，前台按 id 配对）。"""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        on_event: EventSink | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        spawn: Callable[[Sequence[str]], ProcessLike] | None = None,
    ) -> None:
        self.command = list(command)
        self._cwd = str(cwd) if cwd is not None else None
        self._env = env if env is not None else default_env()
        self._on_event = on_event
        self.timeout = timeout
        self._spawn = spawn
        self._proc: ProcessLike | None = None
        self._lock = threading.Lock()
        self._next_id = 0
        #: 等响应的信箱：`id -> Queue`。**一把锁只保护"分配 id + 写请求"**，
        #: 不覆盖等待，否则并发调用会互相排队（真机症状：生成中点资料库/点停止没反应）。
        self._pending: dict[int, queue.Queue[Any]] = {}
        #: 响应比信箱先到的极少数情况（测试里预置响应、或后端快得离谱）——按 id 暂存待取
        self._orphans: dict[int, dict[str, Any]] = {}

    # ------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self._proc is not None:
            return
        try:
            if self._spawn is not None:
                self._proc = self._spawn(self.command)
            else:
                self._proc = subprocess.Popen(
                    self.command,
                    cwd=self._cwd,
                    env=self._env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    # 一个坏字节不该弄死整条通道：解码失败降级成 U+FFFD。
                    # 真机上就是"某行含 GBK 字节 → UnicodeDecodeError → 读线程退出 →
                    # 界面显示'后端进程已退出'"，而其实后端好好的。
                    errors="replace",
                    bufsize=1,
                )
        except OSError as exc:
            raise RpcCallError(f"无法启动后端进程：{exc}") from exc
        threading.Thread(target=self._read_loop, name="scout-rpc-reader", daemon=True).start()

    def _read_loop(self) -> None:
        stream = self._proc.stdout if self._proc is not None else None
        while stream is not None:
            try:
                line = stream.readline()
            except (ValueError, OSError):
                break
            if not line:
                break
            self._route_line(line)
        self._route_eof()

    def _route_line(self, line: str) -> None:
        """把一行分派给"等它的人"：通知进事件回调，响应进对应 id 的信箱。"""
        text = str(line).strip()
        if not text:
            return
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            return  # 后端被第三方库的日志污染，不该把界面搞崩
        if not isinstance(message, dict):
            return
        if "id" not in message:
            name = message.get("method")
            params = message.get("params")
            if isinstance(name, str) and self._on_event is not None:
                self._on_event(name, params if isinstance(params, dict) else {})
            return
        request_id = message.get("id")
        with self._lock:
            box = self._pending.get(request_id) if isinstance(request_id, int) else None
            if box is None:
                if isinstance(request_id, int):
                    self._orphans[request_id] = message
                    while len(self._orphans) > 32:  # 没人认领的响应不能无限堆积
                        self._orphans.pop(next(iter(self._orphans)))
                return
        box.put(message)

    def _route_eof(self) -> None:
        with self._lock:
            boxes = list(self._pending.values())
        for box in boxes:
            box.put(_EOF)

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ----------------------------------------------------------------- 调用

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        """调用一个方法。**可以在已有调用未返回时再调用**（各自按 id 收响应）。"""
        if self._proc is None:
            raise RpcCallError("后端未启动")
        box: queue.Queue[Any] = queue.Queue(maxsize=1)
        with self._lock:  # 只保护"分配 id + 写请求 + 登记信箱"，不覆盖等待
            self._next_id += 1
            request_id = self._next_id
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            if not self.alive:
                raise RpcCallError("后端进程已退出")
            try:
                self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                raise RpcCallError(f"写入后端失败：{exc}") from exc
            self._pending[request_id] = box
            orphan = self._orphans.pop(request_id, None)
        if orphan is not None:
            with self._lock:
                self._pending.pop(request_id, None)
            return _result_of(orphan)
        try:
            return self._await(box, self.timeout if timeout is None else timeout)
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _await(self, box: queue.Queue[Any], timeout: float) -> Any:
        """只等**自己那个信箱**：别的调用在等什么与本调用无关。"""
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RpcCallError(f"等待后端响应超时（{timeout:.0f} 秒）")
            try:
                message = box.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            if message is _EOF:
                raise RpcCallError("后端进程已退出（输出结束）")
            return _result_of(message)

    def close(self, *, timeout: float = 5.0) -> None:
        """关掉 stdin 让后端自然退出；不肯退就 terminate。"""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass
        if proc.poll() is None:
            try:
                proc.wait(timeout=timeout)
            except Exception:
                proc.terminate()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=timeout)

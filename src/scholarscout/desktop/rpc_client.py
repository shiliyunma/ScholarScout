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
    """子进程环境：强制 UTF-8，并把 ``src/`` 挂进 PYTHONPATH（源码运行也能起）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    src = Path(__file__).resolve().parents[2]
    existing = env.get("PYTHONPATH", "")
    parts = [str(src)] + ([existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def default_command(*, db_path: str | Path | None = None, toolsets: str = "research") -> list[str]:
    """后端命令：与手工跑 ``--rpc`` 完全一致，壳不搞特殊通道。"""
    command = [sys.executable, "-m", "scholarscout", "--rpc", "--toolsets", toolsets]
    if db_path is not None:
        command += ["--db", str(db_path)]
    return command


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
        self._lines: queue.Queue[Any] = queue.Queue()
        self._lock = threading.Lock()
        self._next_id = 0

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
            self._lines.put(line)
        self._lines.put(_EOF)

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ----------------------------------------------------------------- 调用

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        if self._proc is None:
            raise RpcCallError("后端未启动")
        with self._lock:
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
            return self._await(request_id, self.timeout if timeout is None else timeout)

    def _await(self, request_id: int, timeout: float) -> Any:
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RpcCallError(f"等待后端响应超时（{timeout:.0f} 秒）")
            try:
                item = self._lines.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            if item is _EOF:
                raise RpcCallError("后端进程已退出（输出结束）")
            text = str(item).strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue

            if "id" not in message:  # 通知：转给事件回调，继续等自己那条响应
                name = message.get("method")
                params = message.get("params")
                if isinstance(name, str) and self._on_event is not None:
                    self._on_event(name, params if isinstance(params, dict) else {})
                continue

            if message.get("id") != request_id:
                continue  # 别人的响应（正常情况下串行化后不会出现）
            error = message.get("error")
            if isinstance(error, dict):
                raise RpcCallError(
                    str(error.get("message") or "后端返回错误"),
                    code=int(error.get("code") or -32000),
                )
            return message.get("result")

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

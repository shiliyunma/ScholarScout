"""``py.exec`` 的执行器：**独立子进程 + Windows Job Object + 子进程内 audit hook**（docs/24）。

一次调用 = 一次新进程，三道约束，各有明确职责：

1. **独立子进程**：``sys.executable -I -c <driver>``，``cwd = workdir``，``-I`` 拿掉用户 site 与
   ``cwd`` 的 ``sys.path`` 注入。工作区外的文件因此不会被"顺手"写进当前解释器的搜索路径。
2. **Windows Job Object**（纯 ``ctypes``，零新依赖）：内存上限（``JOB_OBJECT_LIMIT_PROCESS_MEMORY``）、
   进程数上限（``JOB_OBJECT_LIMIT_ACTIVE_PROCESS``）、``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``。
   超时/父进程收尾时 ``TerminateJobObject`` —— **杀的是整棵树**，不是那一个 PID（子进程再 fork
   出来的进程也在 job 里，核弹式收口）。
3. **子进程内 ``sys.addaudithook``**：``open`` / ``socket.*`` / ``subprocess.*`` / ``ctypes.*`` /
   ``os.system`` / ``os.exec*`` / ``os.spawn*`` 一律先过一道闸；``open`` 只允许
   **规范化（``realpath``）之后**落在工作目录内的路径，读操作额外允许 Python 标准库与
   ``site-packages``（否则子进程连 ``import json`` 都过不去）。被拦下的调用记进 ``violations``
   并当场 ``RuntimeError``。

**这三道加起来仍然不是安全边界**（docs/24 §2）：audit hook 是"同一进程内的一道检查"，
用户在确认框里点的"允许"才是真正的主护栏。任何"这条路一定跑不出去"的说法都是错的 ——
本模块只保证「模型随手写的越界代码会被挡住并留下记录」，不保证「有恶意的本地代码跑不出去」。

**为什么 ``..`` 与符号链接不必特判**：判据不是"字符串里有没有 ``..``"，而是
``os.path.realpath`` 之后再 ``os.path.commonpath``／前缀相等 —— ``../../etc/passwd``、
指向工作区外的 junction/symlink、大小写变形（``C:\\WINDOWS``）在规范化那一步就已经落到真实路径上。
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 单次执行的时间上限（秒）。``run_python`` 会把外部传进来的值夹到这个区间里。
DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 120.0
#: 子进程内存上限（MB）。
DEFAULT_MEM_MB = 512
#: 单路输出（stdout / stderr 各自）保留的字符数，超出截断并标 ``truncated``。
DEFAULT_MAX_OUTPUT = 20_000
#: job 里的进程数上限（子进程自己算 1 个；再 fork 出 3 个就顶住）。
MAX_PROCESSES = 4

#: ``subprocess.CREATE_NO_WINDOW``；写成字面量是为了在非 Windows 上也能 import 本模块。
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

#: Job Object 的 limit flags（``winnt.h``）
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

#: ``OpenProcess`` 需要的权限位：``PROCESS_SET_QUOTA`` | ``PROCESS_TERMINATE`` | ``PROCESS_QUERY_INFORMATION``
_PROCESS_RIGHTS = 0x0100 | 0x0001 | 0x0400


@dataclass(frozen=True)
class ExecResult:
    """一次 ``py.exec`` 的完整结果 —— **成败都同形**，调用方不必先判分支。

    ``ok`` 的判据是「进程正常退出（``returncode == 0``）且没被超时/熔断杀掉」，
    不是「stdout 非空」：写了半天没打印但文件改对了，也是一次成功的执行。
    """

    ok: bool
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    killed: bool
    truncated: bool
    #: audit hook 拦下的调用：``{"event": "open", "detail": "w C:\\Windows\\x"}``
    violations: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """给工具信封 / 测试用的扁平字典（键序固定）。"""
        return {
            "ok": self.ok,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "killed": self.killed,
            "truncated": self.truncated,
            "violations": [dict(item) for item in self.violations],
        }


# --------------------------------------------------------------------------- #
# 子进程驱动脚本（字符串形式，随 -c 传进去；不落盘到工作目录）
# --------------------------------------------------------------------------- #

#: 驱动脚本：读 payload → 开结果文件 → 装 audit hook → exec 用户代码 → 写回结果。
#:
#: **顺序是刻意的**：payload 与结果文件都在工作目录之外，所以必须在**装 hook 之前**打开
#: （hook 装上以后，那条 ``open`` 自己也会被拦）。执行完往已打开的句柄里写，不再触发 ``open``。
_DRIVER = r"""
import builtins
import json
import os
import sys
import traceback

_BLOCKED_EVENTS = frozenset(
    {
        "os.system",
        "os.exec",
        "os.spawn",
        "os.posix_spawn",
        "os.fork",
        "os.forkpty",
        "os.startfile",
        "pty.spawn",
    }
)


def _norm(path):
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _inside(text, roots):
    real = _norm(text)
    for base in roots:
        if real == base:
            return True
        try:
            if os.path.commonpath([real, base]) == base:
                return True
        except ValueError:
            continue
    return False


def _install(root, lib_roots, violations):
    allowed_read_roots = (root,) + tuple(lib_roots)

    def note(event, detail):
        if len(violations) < 20:
            violations.append({"event": event, "detail": str(detail)[:300]})

    def reject(event, detail, hint):
        note(event, detail)
        raise RuntimeError("沙箱拒绝：" + hint)

    def hook(event, args):
        if event == "open":
            target = args[0] if args else None
            mode = args[1] if len(args) > 1 else "r"
            if isinstance(target, int):
                return
            if not isinstance(mode, str):
                mode = "r"
            try:
                inside_workspace = _inside(target, (root,))
                readable = inside_workspace or _inside(target, allowed_read_roots)
            except (TypeError, ValueError):
                reject(event, "无法规范化的路径", "open 的参数不是路径")
                return
            if any(flag in mode for flag in "wax+") and not inside_workspace:
                reject(event, mode + " " + str(target), "只允许写工作目录内的文件：" + str(target))
            if not readable:
                reject(event, mode + " " + str(target), "只允许读工作目录与 Python 标准库内的文件：" + str(target))
            return
        if event.startswith("socket."):
            reject(event, "", "禁止网络访问（" + event + "）")
        if event.startswith("subprocess."):
            reject(event, "", "禁止起子进程（" + event + "）")
        if event.startswith("ctypes."):
            reject(event, "", "禁止原生调用（" + event + "）")
        if event in _BLOCKED_EVENTS:
            reject(event, "", "禁止系统命令与进程替换（" + event + "）")
        if event == "os.chdir":
            target = args[0] if args else None
            try:
                inside = _inside(target, (root,))
            except (TypeError, ValueError):
                inside = False
            if not inside:
                reject(event, str(target), "只允许切到工作目录内的子目录：" + str(target))

    sys.addaudithook(hook)


def _main():
    payload_path = sys.argv[1]
    result_path = sys.argv[2]
    # ``-I`` 会忽略所有 PYTHON* 环境变量（含 PYTHONIOENCODING），所以编码只能在进程内定：
    # 管道默认按系统代码页（中文 Windows 是 cp936），不改成 utf-8 的话中文输出会乱码/报错。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    with open(payload_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    sink = open(result_path, "w", encoding="utf-8")
    root = _norm(payload["workdir"])
    lib_roots = []
    for entry in [sys.prefix, sys.base_prefix] + [item for item in sys.path if item]:
        try:
            lib_roots.append(_norm(entry))
        except (TypeError, ValueError):
            continue

    result = {"ok": False, "returncode": 1, "violations": [], "error": ""}
    violations = []
    status = 0
    error = ""
    _install(root, lib_roots, violations)
    try:
        exec(
            compile(payload["code"], "<py.exec>", "exec"),
            {"__name__": "__main__", "__builtins__": builtins},
        )
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            status = code
        else:
            status = 0 if code is None else 1
    except BaseException:
        status = 1
        error = traceback.format_exc()
        sys.stderr.write(error)
    finally:
        result["ok"] = status == 0
        result["returncode"] = status
        result["violations"] = violations
        result["error"] = error[-4000:]
        try:
            sink.write(json.dumps(result, ensure_ascii=False))
            sink.flush()
        finally:
            sink.close()
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
    sys.exit(status)


_main()
"""


# --------------------------------------------------------------------------- #
# Job Object（Windows）；非 Windows 上退化成"杀一个 PID"
# --------------------------------------------------------------------------- #


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_KERNEL32: ctypes.CDLL | None = (
    ctypes.CDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None
)
if _KERNEL32 is not None:  # pragma: no cover - 只在非 Windows 上不执行
    _KERNEL32.CreateJobObjectW.restype = ctypes.c_void_p
    _KERNEL32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    _KERNEL32.SetInformationJobObject.restype = ctypes.c_int
    _KERNEL32.SetInformationJobObject.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    _KERNEL32.AssignProcessToJobObject.restype = ctypes.c_int
    _KERNEL32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    _KERNEL32.TerminateJobObject.restype = ctypes.c_int
    _KERNEL32.TerminateJobObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    _KERNEL32.OpenProcess.restype = ctypes.c_void_p
    _KERNEL32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    _KERNEL32.CloseHandle.restype = ctypes.c_int
    _KERNEL32.CloseHandle.argtypes = (ctypes.c_void_p,)


class JobObject:
    """Job Object 的薄封装：建 → 设限 → 收编 → 杀 / 关，四件事各一个方法。

    ``handle is None`` 表示这台机器上没建成（非 Windows、或 API 失败）——
    调用方据此退回 ``Popen.kill()``：**降级只会少杀几层，不会多放行**。
    """

    def __init__(self, *, mem_mb: int, max_processes: int = MAX_PROCESSES) -> None:
        self.handle: int | None = None
        self.kill_on_close = False
        if _KERNEL32 is None:
            return
        handle = _KERNEL32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        )
        info.BasicLimitInformation.ActiveProcessLimit = max(1, int(max_processes))
        info.ProcessMemoryLimit = max(1, int(mem_mb)) * 1024 * 1024
        applied = _KERNEL32.SetInformationJobObject(
            ctypes.c_void_p(handle),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not applied:
            _KERNEL32.CloseHandle(ctypes.c_void_p(handle))
            return
        self.handle = int(handle)
        self.kill_on_close = True

    def assign(self, pid: int) -> bool:
        """把子进程收进 job。进程已经退出 / 权限不足时返回 False（调用方走降级路径）。"""
        if self.handle is None or _KERNEL32 is None:
            return False
        process = _KERNEL32.OpenProcess(_PROCESS_RIGHTS, False, pid)
        if not process:
            return False
        try:
            inside = _KERNEL32.AssignProcessToJobObject(
                ctypes.c_void_p(self.handle), ctypes.c_void_p(process)
            )
            return bool(inside)
        finally:
            _KERNEL32.CloseHandle(ctypes.c_void_p(process))

    def terminate(self) -> bool:
        """杀整棵树（job 里所有进程，含子进程的子进程）。"""
        if self.handle is None or _KERNEL32 is None:
            return False
        return bool(_KERNEL32.TerminateJobObject(ctypes.c_void_p(self.handle), 1))

    def close(self) -> None:
        """关句柄 —— 带着 ``KILL_ON_JOB_CLOSE``，关掉就是把残留进程也一并收掉。"""
        if self.handle is not None and _KERNEL32 is not None:
            _KERNEL32.CloseHandle(ctypes.c_void_p(self.handle))
        self.handle = None


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #


def _child_env(scratch: Path) -> dict[str, str]:
    r"""子进程环境：**白名单式**重建，不带用户配置与 token。

    只留 Windows / Python 起得来必需的那几个键（``SYSTEMROOT`` 缺了连 Python 都起不来）；
    ``TEMP`` 指到本次的临时目录；**不设 PYTHON\* 变量** —— ``-I`` 会全部忽略它们，
    设了只会让人以为生效了（编码在驱动脚本里 ``reconfigure``，见 ``_DRIVER``）。
    """
    keep = {
        "PATH",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    env["TEMP"] = str(scratch)
    env["TMP"] = str(scratch)
    return env


def _read_child_result(path: Path) -> dict[str, Any]:
    """读子进程写回的结果；文件缺失或坏掉都退化成 ``{}``（不让收尾逻辑抛异常）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _violations(value: Any) -> list[dict[str, str]]:
    """把子进程回传的 violations 收敛成 ``list[dict[str, str]]``（坏形状直接丢掉）。"""
    if not isinstance(value, list):
        return []
    items: list[dict[str, str]] = []
    for entry in value:
        if isinstance(entry, dict):
            items.append({str(k): str(v) for k, v in entry.items()})
    return items


def _decode(raw: bytes | None, *, limit: int) -> tuple[str, bool]:
    """解码一路输出并截断，返回 ``(文本, 是否被截断)``。"""
    text = (raw or b"").decode("utf-8", "replace")
    if len(text) > limit:
        return text[:limit], True
    return text, False


def run_python(
    code: str,
    *,
    workdir: Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    mem_mb: int = DEFAULT_MEM_MB,
    max_output: int = DEFAULT_MAX_OUTPUT,
) -> ExecResult:
    """在工作目录里跑一段 Python，返回 :class:`ExecResult`。

    参数一律在这里**夹紧**（超时 1–120s、内存 ≥64MB 且 ≤2048MB、输出 ≥1000 字符）：
    这些值会来自模型的 JSON 参数，不能让它把上限抬到没有。
    """
    target = Path(workdir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    limit = max(1.0, min(float(timeout_s), MAX_TIMEOUT_S))
    mem_limit = max(64, min(int(mem_mb), 2048))
    output_limit = max(1000, int(max_output))

    scratch = Path(tempfile.mkdtemp(prefix="scholarscout-exec-"))
    payload_path = scratch / "payload.json"
    result_path = scratch / "result.json"
    payload_path.write_text(
        json.dumps({"code": code, "workdir": str(target)}, ensure_ascii=False), encoding="utf-8"
    )

    job = JobObject(mem_mb=mem_limit)
    started = time.monotonic()
    timed_out = False
    killed = False
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", _DRIVER, str(payload_path), str(result_path)],
            cwd=str(target),
            env=_child_env(scratch),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
        job.assign(process.pid)
        try:
            out, err = process.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            killed = job.terminate()
            if not killed:
                process.kill()
                killed = True
            out, err = process.communicate()
        duration_ms = int((time.monotonic() - started) * 1000)
        child = _read_child_result(result_path)
    finally:
        job.close()
        shutil.rmtree(scratch, ignore_errors=True)

    stdout_text, stdout_cut = _decode(out, limit=output_limit)
    stderr_text, stderr_cut = _decode(err, limit=output_limit)
    returncode = process.returncode if process.returncode is not None else -1
    return ExecResult(
        ok=returncode == 0 and not timed_out and not killed,
        returncode=returncode,
        stdout=stdout_text,
        stderr=stderr_text,
        duration_ms=duration_ms,
        timed_out=timed_out,
        killed=killed,
        truncated=stdout_cut or stderr_cut,
        violations=_violations(child.get("violations")),
    )

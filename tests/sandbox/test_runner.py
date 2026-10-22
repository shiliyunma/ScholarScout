"""执行器五件事：audit hook 拦什么、``..``/符号链接怎么防、Job Object 限什么、超时怎么杀。

被测对象是**真的子进程**（不是桩）：沙箱的价值全在"进程边界 + 内核对象"上，桩掉这两样就什么都没验到。
所以这里跑的都是本文件里写死的固定小片段（打印一行、写一个小文件、死循环、要一大块内存）。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scholarscout.sandbox.runner import JobObject, run_python

_KERNEL32 = ctypes.CDLL("kernel32", use_last_error=True)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _pid_alive(pid: int) -> bool:
    """进程是否还活着。

    为什么不用 ``os.kill(pid, 0)`` / ``tasklist``：前者在 Windows 上不判存活（对任意 pid 都"成功"），
    后者要走文本解析（代码页还会把输出弄成非 UTF-8，管道解码随时炸）。直接问内核最稳。
    """
    handle = _KERNEL32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not _KERNEL32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        _KERNEL32.CloseHandle(ctypes.c_void_p(handle))


def _wait_gone(pid: int, timeout_s: float = 5.0) -> bool:
    """等一个 PID 消失。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return False


# --------------------------------------------------------------------------- #
# 1. audit hook：工作区内可写；工作区外一律拦并记进 violations
# --------------------------------------------------------------------------- #


def test_write_inside_workdir_is_allowed(tmp_path: Path) -> None:
    """工作目录内正常：写文件 + 打印，``ok`` 且无 violations。"""
    workdir = tmp_path / "work"
    result = run_python(
        'open("note.txt", "w", encoding="utf-8").write("hello")\nprint("done")',
        workdir=workdir,
        timeout_s=30,
    )
    assert result.ok, result.stderr
    assert result.violations == []
    assert (workdir / "note.txt").read_text(encoding="utf-8") == "hello"
    assert "done" in result.stdout


def test_read_outside_workdir_is_blocked_and_recorded(tmp_path: Path) -> None:
    """工作区外**读**也拦：``open`` 的判据是"规范化后是否在工作目录内"。"""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    result = run_python(
        f"print(open({str(outside)!r}, encoding='utf-8').read())",
        workdir=tmp_path / "work",
        timeout_s=30,
    )
    assert not result.ok
    assert result.violations, "越界 open 必须写进 violations，否则就是拦了但没人知道"
    assert result.violations[0]["event"] == "open"
    assert "沙箱拒绝" in result.stderr, "被拦时必须给模型一句可读的拒绝理由"


def test_dotdot_escape_is_blocked(tmp_path: Path) -> None:
    """``..`` 逃逸：判据是 ``realpath`` 之后的前缀，不是字符串里有没有 ``..``。"""
    workdir = tmp_path / "work"
    workdir.mkdir()
    result = run_python(
        'open("../escaped.txt", "w").write("x")',
        workdir=workdir,
        timeout_s=30,
    )
    assert not result.ok
    assert result.violations and result.violations[0]["event"] == "open"
    assert not (tmp_path / "escaped.txt").exists(), "越界写必须真的没发生"


def test_symlink_escape_is_blocked(tmp_path: Path) -> None:
    """符号链接逃逸：工作目录里的链接指向外面，也要按**真实路径**判。"""
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    link = workdir / "link.txt"
    try:
        os.symlink(outside, link)
    except OSError as exc:  # pragma: no cover - 本机（Windows 开发者模式）能建符号链接
        pytest.skip(f"本机不允许创建符号链接，这条只能靠 .. 那条兜：{exc}")
    result = run_python(
        'print(open("link.txt", encoding="utf-8").read())', workdir=workdir, timeout_s=30
    )
    assert not result.ok
    assert result.violations and result.violations[0]["event"] == "open"


def test_network_subprocess_and_native_are_blocked(tmp_path: Path) -> None:
    """``socket.*`` / ``subprocess.*`` / ``ctypes.*`` 三类事件各拦一次，且都留痕。"""
    workdir = tmp_path / "work"
    cases = (
        ("import socket\nsocket.socket()", "socket."),
        ('import subprocess\nsubprocess.run(["cmd", "/c", "echo hi"])', "subprocess."),
        ('import ctypes\nctypes.CDLL("kernel32")', "ctypes."),
    )
    for code, prefix in cases:
        result = run_python(code, workdir=workdir, timeout_s=30)
        assert not result.ok, f"{prefix} 竟然跑通了：{result.stdout}"
        assert result.violations, f"{prefix} 没有留痕"
        assert result.violations[0]["event"].startswith(prefix)


# --------------------------------------------------------------------------- #
# 2. Job Object：内存上限、超时杀整棵树
# --------------------------------------------------------------------------- #


def test_job_object_reports_its_limits(tmp_path: Path) -> None:
    """Job Object 真的建起来了（非 Windows 上会退化成 ``handle is None``）。"""
    job = JobObject(mem_mb=256, max_processes=4)
    try:
        if sys.platform != "win32":  # pragma: no cover - 本机是 Windows
            assert job.handle is None
        else:
            assert job.handle is not None, "Job Object 没建起来：内存与进程数上限就都没了"
            assert job.kill_on_close is True
    finally:
        job.close()


def test_memory_limit_kills_the_child_but_not_the_parent(tmp_path: Path) -> None:
    """内存超限：子进程死在 128MB 上限上，父进程（本测试）照常拿到结果。"""
    result = run_python(
        "block = bytearray(600 * 1024 * 1024)\nprint(len(block))",
        workdir=tmp_path / "work",
        timeout_s=30,
        mem_mb=128,
    )
    assert not result.ok
    assert result.returncode != 0
    assert "MemoryError" in result.stderr, f"没有死在内存上限上：{result.stderr[-300:]}"


def test_timeout_kills_the_process_tree(tmp_path: Path) -> None:
    """超时 → ``timed_out`` 且**整棵树**被杀（Job Object 的 ``TerminateJobObject``）。

    两段验证：
    1. 走 ``run_python`` 的死循环，拿到 ``timed_out=True, killed=True`` 与冒烟级的退出码；
    2. 单独验 Job Object 的"树"：父进程再起一个孙进程，两个都在同一个 job 里，
       ``terminate()`` 之后**两个 PID 都消失** —— 这才是"杀树"而不是"杀一个 PID"的证据。
    """
    result = run_python("while True:\n    pass", workdir=tmp_path / "work", timeout_s=2)
    assert result.timed_out is True
    assert result.killed is True
    assert not result.ok
    assert 1000 <= result.duration_ms <= 15000, (
        f"超时控制在 2s 左右才算生效：{result.duration_ms}ms"
    )

    if sys.platform != "win32":  # pragma: no cover - 本机是 Windows
        pytest.skip("Job Object 只在 Windows 上有；这条验的就是它的杀树行为")

    grandchild_code = "import time\ntime.sleep(60)"
    child_code = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-I', '-c', {grandchild_code!r}])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-I", "-c", child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    job = JobObject(mem_mb=512)
    try:
        assert job.assign(parent.pid) is True, "子进程没能收进 job"
        assert parent.stdout is not None
        grandchild_pid = int(parent.stdout.readline().strip())
        assert _wait_gone(grandchild_pid, timeout_s=0.1) is False, "孙进程一开始就该活着"
        assert job.terminate() is True
        parent.wait(timeout=10)
        assert _wait_gone(grandchild_pid), "孙进程应该跟着 job 一起被杀"
    finally:
        job.close()
        if parent.poll() is None:
            parent.kill()
        if parent.stdout is not None:
            parent.stdout.close()


# --------------------------------------------------------------------------- #
# 3. 输出：stdout/stderr 分开、超长截断
# --------------------------------------------------------------------------- #


def test_output_is_separated_and_truncated(tmp_path: Path) -> None:
    """stdout 与 stderr 分开回，超长按 ``max_output`` 截断并标 ``truncated``。"""
    code = 'import sys\nsys.stdout.write("A" * 5000)\nsys.stderr.write("B" * 5000)\n'
    result = run_python(code, workdir=tmp_path / "work", timeout_s=30, max_output=1000)
    assert result.ok
    assert result.truncated is True
    assert len(result.stdout) == 1000
    assert len(result.stderr) == 1000
    assert set(result.stdout) == {"A"}
    assert set(result.stderr) == {"B"}


def test_failure_keeps_traceback_in_stderr(tmp_path: Path) -> None:
    """用户代码抛异常 → ``ok=false`` + 回溯在 stderr（模型据此改代码）。"""
    result = run_python("raise ValueError('boom')", workdir=tmp_path / "work", timeout_s=30)
    assert not result.ok
    assert result.returncode == 1
    assert "ValueError: boom" in result.stderr

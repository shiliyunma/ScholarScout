"""渠道**子进程 supervisor** 的测试：起停 / pid / 日志 / 状态快照 / 精准停（docs/21 §4）。

分三层，都不是"看起来对"：

1. **真子进程**（`:file:`_channel_child_stub.py`）：真 `Popen` 起、真 RPC 桥、真 `taskkill /F /PID`
   停 —— 桩子进程会把"它调过一次 RPC 并拿到了结果"写进状态快照，所以这条链路是**验过的**；
2. **命令形状**：`taskkill /F /PID <pid>` 且显式带 `MSYS_NO_PATHCONV=1`（docs/17 §3.9 的坑：
   bash 下会把 `/F` 当路径改写掉、taskkill 报"无效参数"）；
3. **纪律**：`stop` 没在跑时**不假装成功**；`pid_alive` 不许用 `os.kill(pid, 0)` 当探针
   （Windows 上那不是探测，是 `TerminateProcess`）。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from scholarscout.channels import remote, supervisor
from scholarscout.rpc.methods import AppContext

from ..conftest import redirect_home

STUB = Path(__file__).resolve().parent / "_channel_child_stub.py"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run/` 重定向到 tmp；每个用例结束**确保没有残留子进程**。"""
    redirect_home(monkeypatch, tmp_path)
    yield
    supervisor.stop("feishu", wait_s=5.0)


def _stub_command(platform: str) -> list[str]:
    return [sys.executable, str(STUB), platform]


def _wait_for(predicate, *, timeout: float = 20.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


# ------------------------------------------------------------------ 路径与 pid


def test_runtime_files_live_under_run_channels() -> None:
    """pid / log / state 三件都落在 `run/channels/<platform>.{pid,log,state.json}`（一处定义）。"""
    assert supervisor.pid_path("feishu").name == "feishu.pid"
    assert supervisor.log_path("feishu").name == "feishu.log"
    assert supervisor.state_path("feishu").name == "feishu.state.json"
    assert supervisor.channel_dir().name == "channels"
    assert supervisor.channel_dir().parent.name == "run"


def test_only_feishu_is_supported_this_round() -> None:
    """本轮只做飞书（docs/21 §8 的微信/QQ 是从零接，**不写它们任何通道代码**）。"""
    assert supervisor.PLATFORMS == ("feishu",)
    for name in ("wechat", "qq", "weixin"):
        with pytest.raises(ValueError):
            supervisor.ensure_supported(name)


def test_pid_alive_probes_without_killing() -> None:
    """`pid_alive` 只探测不动手：探自己之后**自己还活着**。

    这条钉的是 Windows 的坑：`os.kill(pid, 0)` 在 Windows 上会走 `TerminateProcess`，
    拿它当探针等于顺手把进程杀掉（真踩过这类"探针即凶器"）。
    """
    me = os.getpid()
    assert supervisor.pid_alive(me) is True
    assert supervisor.pid_alive(me) is True, "第二次探测仍然只探测"
    assert supervisor.pid_alive(0) is False
    assert supervisor.pid_alive(2**22) is False, "不存在的 pid 必须是 False"


def test_stale_pid_file_does_not_count_as_running(tmp_path: Path) -> None:
    """陈旧 pid 文件（上次崩溃留下的）不算"在跑"。"""
    supervisor.write_pid("feishu", 2**22)
    assert supervisor.read_pid("feishu") == 2**22
    assert supervisor.live_pid("feishu") is None
    assert supervisor.snapshot("feishu")["running"] is False


# ------------------------------------------------------------------ 命令形状


def test_kill_uses_taskkill_with_msys_no_pathconv() -> None:
    """按 PID 精准停：`taskkill /F /PID <pid>` + `MSYS_NO_PATHCONV=1`（不许按名字杀）。"""
    seen: dict[str, object] = {}

    class _Done:
        returncode = 0

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        seen["command"] = command
        seen["env"] = kwargs.get("env")
        return _Done()

    assert supervisor.kill_pid(4242, runner=fake_run) is True
    assert seen["command"] == ["taskkill", "/F", "/PID", "4242"]
    env = seen["env"]
    assert isinstance(env, dict) and env["MSYS_NO_PATHCONV"] == "1"


def test_child_command_runs_the_same_launcher_as_manual(monkeypatch: pytest.MonkeyPatch) -> None:
    """子进程命令与手工跑同形：`python scripts/scout.py --channel feishu`（不搞特殊通道）。"""
    monkeypatch.setenv("SCHOLARSCOUT_HOME", str(Path(__file__).resolve().parents[2]))
    command = supervisor.child_command("feishu")
    assert command[0] == sys.executable
    assert command[1].endswith("scout.py") and "scripts" in command[1]
    assert command[-2:] == ["--channel", "feishu"]


def test_child_command_falls_back_when_launcher_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """找不到启动器时退回 `-m scholarscout`（而不是拼一条不存在的路径后静默失败）。"""
    redirect_home(monkeypatch, tmp_path)
    command = supervisor.child_command("feishu")
    assert command[:3] == [sys.executable, "-m", "scholarscout"]
    assert command[-2:] == ["--channel", "feishu"]


def test_child_env_inherits_process_env_and_forces_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """凭据随**继承的环境**传下去（不落第二份副本），并强制 UTF-8（中文路径的既有坑）。"""
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_inherited")
    env = supervisor.child_env()
    assert env["FEISHU_APP_SECRET"] == "sec_inherited"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"


# ------------------------------------------------------------------ 状态快照


def test_state_file_is_atomic_and_merges(tmp_path: Path) -> None:
    supervisor.write_state("feishu", {"state": "running", "reconnects": 1})
    supervisor.write_state("feishu", {"connected": True})
    state = supervisor.read_state("feishu")
    assert state["state"] == "running" and state["reconnects"] == 1 and state["connected"] is True
    assert state["updated_at"] and state["platform"] == "feishu"
    assert not list(supervisor.channel_dir().glob("*.tmp")), "临时文件必须已被 os.replace 吃掉"


def test_snapshot_reports_stopped_without_a_process() -> None:
    snap = supervisor.snapshot("feishu")
    assert snap["running"] is False and snap["pid"] is None
    assert snap["state"] == "stopped" and snap["connected"] is False


def test_snapshot_keeps_error_state_for_the_ui() -> None:
    """子进程报了错、人也没再点启动 → 界面仍要看到 error 与原因，不能悄悄变成"没配过"。"""
    supervisor.write_state("feishu", {"state": "error", "last_error": "凭据未配置"})
    snap = supervisor.snapshot("feishu")
    assert snap["state"] == "error" and snap["last_error"] == "凭据未配置"


def test_read_state_never_raises_on_garbage() -> None:
    supervisor.channel_dir().mkdir(parents=True, exist_ok=True)
    supervisor.state_path("feishu").write_text("{ 不是 json", encoding="utf-8")
    assert supervisor.read_state("feishu") == {}


# ------------------------------------------------------------------ 起 / 停（真子进程）


def test_start_stop_round_trip_with_a_real_child(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真起一个子进程、真经 RPC 说话、真按 PID 停掉。

    断言四件事：① pid 文件写下且进程活着；② 日志真落 `run/channels/feishu.log`；
    ③ 子进程**真的调通了一次 RPC**（它把结果写进了状态快照）；④ 停完之后 pid 文件清掉、状态回到 stopped。
    """
    monkeypatch.setattr(supervisor, "child_command", _stub_command)
    started = supervisor.start(ctx, "feishu")
    assert started["ok"] is True and started["state"] == "running"
    pid = int(started["pid"])
    assert supervisor.read_pid("feishu") == pid
    assert supervisor.pid_alive(pid) is True

    log = Path(str(started["log_path"]))
    assert _wait_for(lambda: log.is_file() and "rpc ok channels=" in log.read_text("utf-8"))

    # ② 子进程经 RPC 拿到过父进程的答复（桩子进程把它写在状态快照里）
    assert _wait_for(lambda: bool(supervisor.read_state("feishu").get("rpc_probe")))
    assert str(supervisor.read_state("feishu")["rpc_probe"]).startswith("channels=")
    snap = supervisor.snapshot("feishu")
    assert snap["running"] is True and snap["pid"] == pid and snap["state"] == "running"

    stopped = supervisor.stop("feishu")
    assert stopped["ok"] is True and stopped["state"] == "stopped" and stopped["pid"] == pid
    assert _wait_for(lambda: not supervisor.pid_alive(pid)), "按 PID 停之后进程必须真的没了"
    assert supervisor.read_pid("feishu") is None
    assert supervisor.snapshot("feishu")["state"] == "stopped"


def test_start_is_idempotent_and_never_starts_a_second_instance(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor, "child_command", _stub_command)
    first = supervisor.start(ctx, "feishu")
    second = supervisor.start(ctx, "feishu")
    assert second["ok"] is True
    assert second["note"] == "already_running"
    assert second["pid"] == first["pid"], "必须复用同一个子进程，不许起第二个"
    assert supervisor.read_pid("feishu") == first["pid"]


def test_stop_without_a_running_child_does_not_pretend_success() -> None:
    """照 `chat.cancel` 的既有铁律：没在跑就如实回 `not_running`。"""
    result = supervisor.stop("feishu")
    assert result["ok"] is False
    assert result["reason"] == "not_running"
    assert result["state"] == "stopped"


def test_stop_cleans_stale_pid_file() -> None:
    supervisor.write_pid("feishu", 2**22)
    result = supervisor.stop("feishu")
    assert result["ok"] is False and result["reason"] == "not_running"
    assert supervisor.read_pid("feishu") is None, "陈旧 pid 文件要顺手清掉"


def test_kill_failure_is_reported_not_swallowed(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """taskkill 没能停掉时如实回 `kill_failed`，并给出下一步（不许假装成功）。"""
    monkeypatch.setattr(supervisor, "child_command", _stub_command)
    started = supervisor.start(ctx, "feishu")
    result = supervisor.stop("feishu", killer=lambda _pid: False, wait_s=0.2)
    # 兜底会 kill 自己起的那个句柄 → 结果仍然是"停掉了"，但 note 里不许编
    assert result["state"] in ("stopped", "running")
    if result["ok"]:
        assert _wait_for(lambda: not supervisor.pid_alive(int(started["pid"])))
    else:
        assert result["reason"] == "kill_failed"
        supervisor.stop("feishu")


# ------------------------------------------------------------------ 纪律：子进程不开库


def test_child_never_opens_a_database() -> None:
    """**机制**（不是自觉）：子进程侧的上下文里根本没有 `db`。

    docs/20 的单写纪律要求"渠道子进程只当 RPC 客户端、不自己开库"；这里把"接口面"钉住：
    `RemoteContext.db` 是 `None`，且 `channels/` 全包不 import `state` / 不构造 `Database`。
    """
    context = remote.RemoteContext()
    assert context.db is None
    assert context.llm is None and context.http is None
    with pytest.raises(remote.RemoteError):
        context.require_llm()

    package = Path(supervisor.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "from scholarscout.state" in text or "import scholarscout.state" in text:
            offenders.append(f"{path.name} import 了 state 层")
        if "Database(" in text:
            offenders.append(f"{path.name} 直接构造了 Database")
    assert not offenders, "渠道包不许开库：" + "；".join(offenders)

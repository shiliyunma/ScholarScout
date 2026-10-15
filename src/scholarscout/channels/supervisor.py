"""渠道子进程的 supervisor（docs/21 §4 的 B 方案：**独立子进程 + 主进程当 supervisor**）。

## 为什么是独立子进程（取舍照 docs/21 §4 原文，不重写理由）

关窗后渠道要**继续收消息**（飞书长连接不断），渠道进程崩了不能带崩界面，
而且微信/QQ 的"重扫码 / 重新加群"这类贵动作要尽量只做一次 —— 这三条都要求进程隔离。
代价照实说：多一层 IPC、多一类生命周期（pid / 日志 / 残留进程）、"关窗"语义要重新定义。

## 落盘布局（一处定义，别在别处再拼路径）

    run/channels/<platform>.pid         子进程 pid（停止时按它精准停）
    run/channels/<platform>.log         子进程日志（stderr 重定向到这里）
    run/channels/<platform>.state.json  子进程状态快照（**由子进程自己写**，界面读它）

`.pid` / `.log` 的命名沿用 docs/21 §4 的原文；`.state.json` 是本模块加的：
界面要显示"连接 / 重连次数 / 最后一条消息 / 最后一次错误"，而**主进程不能替子进程编这些值** ——
所以由子进程在状态变化时写一份快照，主进程只读，读不到就说"未连接"，不猜。

## 两条纪律

1. **子进程只当 RPC 客户端，不自己开库**（docs/21 §4 末行）：`channels/remote.py` 给的
   `RemoteContext` 里根本没有 `db`；子进程要数据就调父进程的 RPC。这条由
   `tests/channels/test_channel_supervisor.py::test_child_never_opens_a_database` 钉住。
2. **没在跑时 `stop` 不假装成功**：回 `{ok: False, reason: "not_running"}`（照 `chat.cancel` 的既有铁律）。

## Windows 的坑（docs/17 §3.9）

停子进程用 `taskkill /F /PID <pid>`。**bash 里要写 `MSYS_NO_PATHCONV=1`**，否则 MSYS 会把
`/F` 当路径改写掉、taskkill 报"无效参数"。本模块是 Python 直接调 `subprocess`，本不受 MSYS 影响，
但**照样显式带上 `MSYS_NO_PATHCONV=1`** —— 这样从 git-bash 手工复现同一条命令时行为一致。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO, cast

from scholarscout import paths
from scholarscout.channels import feishu

logger = logging.getLogger(__name__)

#: 本轮只做飞书（docs/21 §8 的第 8/9 步是从零接微信/QQ；**本轮不写它们任何代码**）
PLATFORMS: tuple[str, ...] = ("feishu",)
#: 状态快照的合法 state 取值（与 docs/21 §5.1 的 `channel.status.state` 一致）
STATES: tuple[str, ...] = ("stopped", "starting", "running", "error")
#: 渠道运行期文件所在目录名（`run/` 下）
DIR_NAME = "channels"
#: stop 之后等子进程真正退出的上限（秒）
STOP_WAIT_SECONDS = 8.0
#: 子进程环境里强制 UTF-8（中文路径/中文日志的既有坑，见 docs/03 §9）
ENV_UTF8 = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
#: `taskkill` 的候选路径：先按 PATH 找，找不到再按 System32 兜底（不同 shell 的 PATH 不一样）
_TASKKILL_CANDIDATES = ("taskkill", r"C:\Windows\System32\taskkill.exe")

#: 本进程起过的子进程句柄（platform -> Popen）。**跨进程重启后靠 pid 文件**，不靠这张表。
_PROCS: dict[str, subprocess.Popen[str]] = {}
_LOCK = threading.Lock()


# ------------------------------------------------------------------ 平台与路径


def normalize_platform(platform: Any) -> str:
    """平台名归一（只认小写；本轮只支持飞书）。**不认识的平台由调用方报错**，这里只归一。"""
    return str(platform or "").strip().lower()


def ensure_supported(platform: Any) -> str:
    name = normalize_platform(platform)
    if name not in PLATFORMS:
        raise ValueError(f"不支持的平台：{name or '(空)'}（本轮只支持：{'、'.join(PLATFORMS)}）")
    return name


def channel_dir(root: Path | None = None) -> Path:
    """渠道运行期目录 `run/channels/`（**唯一拼路径的地方**）。"""
    return paths.run_dir(root) / DIR_NAME


def pid_path(platform: str, root: Path | None = None) -> Path:
    return channel_dir(root) / f"{platform}.pid"


def log_path(platform: str, root: Path | None = None) -> Path:
    return channel_dir(root) / f"{platform}.log"


def state_path(platform: str, root: Path | None = None) -> Path:
    return channel_dir(root) / f"{platform}.state.json"


# ------------------------------------------------------------------ pid 文件


def read_pid(platform: str, root: Path | None = None) -> int | None:
    """读 pid 文件（读不到 / 不是数字 → None，不抛异常）。"""
    target = pid_path(platform, root)
    try:
        text = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def write_pid(platform: str, pid: int, root: Path | None = None) -> Path:
    target = pid_path(platform, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{pid}\n", encoding="utf-8")
    return target


def remove_pid(platform: str, root: Path | None = None) -> bool:
    target = pid_path(platform, root)
    try:
        target.unlink()
        return True
    except OSError:
        return False


def pid_alive(pid: int) -> bool:
    """这个 pid 现在还活着吗。

    为什么不用 `os.kill(pid, 0)`：**Windows 上它不是"探测"**（CPython 会走
    `TerminateProcess`），拿它当探针等于顺手把对方杀了。所以 Windows 走
    `OpenProcess` + `GetExitCodeProcess`（`STILL_ACTIVE`=259）。
    """
    if pid <= 0:
        return False
    if os.name != "nt":  # pragma: no cover - 本项目只跑 Windows，留一条 POSIX 兜底
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes  # pragma: no cover - 平台相关

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return bool(code.value == still_active)
    finally:
        kernel32.CloseHandle(handle)


def live_pid(platform: str, root: Path | None = None) -> int | None:
    """pid 文件里的 pid（**且确实活着**）。文件在、进程没了 → None（陈旧文件）。"""
    pid = read_pid(platform, root)
    if pid is None:
        return None
    owned = _PROCS.get(platform)
    if owned is not None and owned.pid == pid and owned.poll() is not None:
        return None
    return pid if pid_alive(pid) else None


# ------------------------------------------------------------------ 状态快照


def read_state(platform: str, root: Path | None = None) -> dict[str, Any]:
    """读子进程写的状态快照；没有 / 坏了 → 空字典（**不抛异常、不猜值**）。"""
    try:
        raw = state_path(platform, root).read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def write_state(platform: str, payload: dict[str, Any], root: Path | None = None) -> Path:
    """原子写状态快照（先 `.tmp` 再 `os.replace`，照 `settings_io` 的既有做法）。

    `.tmp` **必须带 pid+线程号**（真机踩到）：状态快照有**多个写者**——`stop()` 与子进程退出后
    收尾的桥线程会同时写（停进程时两者必然相邻），渠道子进程自己也写一份。
    Windows 下固定名字的 `.tmp` 被另一个写者打开着时 `open(mode="w")` 直接 `PermissionError`
    （间歇性、全量跑门禁时不一定复现），加进程号+线程号各写各的即可。
    """
    target = state_path(platform, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    merged = {**read_state(platform, root), **payload}
    merged["platform"] = platform
    merged["updated_at"] = now_iso()
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def snapshot(platform: str, root: Path | None = None) -> dict[str, Any]:
    """界面要的状态行：**主进程能证的（pid/日志）自己算，证不了的（连接/重连/最后消息）读子进程的快照**。"""
    name = ensure_supported(platform)
    pid = live_pid(name, root=root)
    state = read_state(name, root=root)
    if pid is None:
        # 没进程时：子进程报过 error 就仍然显示 error（否则界面会以为"没配过、什么错都没有"）
        derived = "error" if state.get("state") == "error" else "stopped"
    else:
        reported = str(state.get("state") or "")
        derived = reported if reported in STATES and reported != "stopped" else "running"
    return {
        "platform": name,
        "pid": pid,
        "running": pid is not None,
        "state": derived,
        "connected": bool(pid is not None and state.get("connected")),
        "reconnects": int(state.get("reconnects") or 0),
        "last_message_at": state.get("last_message_at"),
        "last_message_from": state.get("last_message_from"),
        "last_error": state.get("last_error"),
        "log_path": str(log_path(name, root)),
    }


# ------------------------------------------------------------------ 起 / 停


def child_command(platform: str) -> list[str]:
    """子进程命令：**与手工跑完全一致**（`python scripts/scout.py --channel feishu`），不搞特殊通道。

    打包后要拉起自己（`sys.executable --channel …`）—— 照 `desktop/rpc_client.default_command`
    的同一条教训：PyInstaller 的引导器不认 `-m 模块`。
    """
    name = ensure_supported(platform)
    if getattr(sys, "frozen", False):  # pragma: no cover - 打包环境
        return [sys.executable, "--channel", name]
    launcher = paths.home() / "scripts" / "scout.py"
    if launcher.is_file():
        return [sys.executable, str(launcher), "--channel", name]
    return [sys.executable, "-m", "scholarscout", "--channel", name]


def child_env() -> dict[str, str]:
    """子进程环境：**继承**父进程（凭据随着环境传下去，不落第二份副本，docs/21 §6）+ 强制 UTF-8。

    源码运行时把 `src/` 挂进 PYTHONPATH；打包后**不挂**（挂上去会让子进程 import 到源码而不是
    打进包的模块，真机症状见 `desktop/rpc_client.default_env` 的注释）。
    """
    env = dict(os.environ)
    env.update(ENV_UTF8)
    if not getattr(sys, "frozen", False):
        src = Path(__file__).resolve().parents[2]
        if src.is_dir():
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join([str(src)] + ([existing] if existing else []))
    return env


def config_env(ctx: Any) -> dict[str, str]:
    """子进程环境 = `child_env()` **+ 把父进程读到的渠道配置翻译成环境变量带下去**。

    为什么必须有这一步（docs/17 §3.11）：`runtime.feishu_ack_text` 住在 `config.yaml` 里，
    而渠道子进程**没有配置对象**（它只当 RPC 客户端、不读 config、不开库，见模块顶注的纪律 1），
    所以父进程必须在起它的时候把值翻译成环境变量 —— 与凭据 / `FEISHU_DM_POLICY` 走同一条路
    （环境继承，不落第二份副本）。**空串也要注入**：它是"关掉回执"的合法取值，
    用 `or` 兜默认会把用户显式关掉的选择吃掉。
    读不到配置（`ctx` 没有 `config`）时**一个键都不注入**：那时渠道层走自己的缺省，不编值。
    """
    env = child_env()
    config = getattr(ctx, "config", None)
    text = getattr(getattr(config, "runtime", None), "feishu_ack_text", None)
    if isinstance(text, str):
        env[feishu.ACK_TEXT_ENV] = text
    return env


def start(
    ctx: Any,
    platform: str,
    *,
    root: Path | None = None,
    command: Sequence[str] | None = None,
    env: dict[str, str] | None = None,
    #: 注入用：形如 `bridge_to_parent(ctx, proc, *, platform, root)` 的工厂（测试给桩）
    bridge_factory: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """起渠道子进程。**幂等**：已在跑就直接回 `already_running`，绝不起第二个实例。"""
    name = ensure_supported(platform)
    log = log_path(name, root)
    log.parent.mkdir(parents=True, exist_ok=True)
    existing = live_pid(name, root=root)
    if existing is not None:
        return {
            "ok": True,
            "state": "running",
            "platform": name,
            "pid": existing,
            "log_path": str(log),
            "note": "already_running",
        }

    cmd = list(command) if command is not None else child_command(name)
    handle = open(log, "a", encoding="utf-8")  # noqa: SIM115 - 生命周期交给 Popen（stderr）
    try:
        proc: subprocess.Popen[str] = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=handle,
            env=env if env is not None else config_env(ctx),
            cwd=str(root or paths.home()),
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
    except OSError as exc:
        handle.close()
        write_state(name, {"state": "error", "last_error": f"起子进程失败：{exc}"}, root)
        return {"ok": False, "state": "error", "reason": str(exc), "log_path": str(log)}

    with _LOCK:
        _PROCS[name] = proc
    write_pid(name, proc.pid, root)
    write_state(name, {"state": "starting", "pid": proc.pid, "last_error": None}, root)
    (bridge_factory or bridge_to_parent)(ctx, proc, platform=name, root=root)
    emit_state(ctx, {"platform": name, "state": "running", "pid": proc.pid})
    logger.info("[Channel] 已起 %s 子进程 pid=%s 日志=%s", name, proc.pid, log)
    return {
        "ok": True,
        "state": "running",
        "platform": name,
        "pid": proc.pid,
        "log_path": str(log),
    }


def bridge_to_parent(
    ctx: Any, proc: subprocess.Popen[str], *, platform: str, root: Path | None = None
) -> None:
    """在后台线程里当子进程的 **RPC 服务端**：读它一行请求 → 分发 → 写回。

    为什么能直接复用 `rpc/server.serve`：传输形状完全相同（一行一个 JSON），只是"stdin/stdout"
    换成了子进程的管道。分发**只有一份**，子进程因此拿到与界面一样的后端能力。
    """
    from scholarscout.rpc import server

    def _serve() -> None:
        child_in = proc.stdout
        child_out = proc.stdin
        if child_in is None or child_out is None:  # pragma: no cover - PIPE 下必非 None
            logger.warning("[Channel] %s 的子进程管道不可用，RPC 桥不启动", platform)
            _on_child_exit(platform, proc, root=root)
            return
        try:
            # `cast` 只为对齐类型：mypy 把 `Popen[str].stdout` 推成 `IO[str]`（typeshed 的
            # `Popen[str]` 重载怪癖），而 `text=True` 起出来的这个管道**语义上就是** `TextIO`
            # —— 逐行读的是 str，不是 bytes。不改 `serve()` 的签名去迁就它。
            server.serve(ctx, stdin=child_in, stdout=cast("TextIO", child_out), max_workers=2)
        except Exception as exc:  # pragma: no cover - 管道断了等罕见路径
            logger.warning("[Channel] %s 的 RPC 桥退出：%s", platform, exc)
        finally:
            _on_child_exit(platform, proc, root=root)

    threading.Thread(target=_serve, name=f"channel-bridge-{platform}", daemon=True).start()


def _on_child_exit(platform: str, proc: subprocess.Popen[str], *, root: Path | None = None) -> None:
    """子进程退出后收尾：摘句柄、清 pid 文件、把状态落成 stopped（**已报 error 的保留 error**）。"""
    with _LOCK:
        if _PROCS.get(platform) is proc:
            _PROCS.pop(platform, None)
    if read_pid(platform, root) == proc.pid:
        remove_pid(platform, root=root)
    current = read_state(platform, root=root)
    if current.get("state") != "error":
        write_state(platform, {"state": "stopped", "connected": False}, root)
    logger.info("[Channel] %s 子进程已退出：pid=%s code=%s", platform, proc.pid, proc.poll())


def kill_pid(pid: int, *, runner: Callable[..., Any] | None = None) -> bool:
    """按 PID 精准停（**不用 `pkill`/按名字杀**，免得误伤别的监听进程）。

    Windows：`taskkill /F /PID <pid>`，显式带 `MSYS_NO_PATHCONV=1`（见模块顶注）。
    非 Windows 兜底 `SIGTERM`（本项目只在 Windows 上跑，留一条不至于崩）。
    """
    if pid <= 0:
        return False
    run = runner or subprocess.run
    if os.name != "nt":  # pragma: no cover - POSIX 兜底
        import signal

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        return True
    env = dict(os.environ)
    env["MSYS_NO_PATHCONV"] = "1"
    for candidate in _TASKKILL_CANDIDATES:
        try:
            completed = run(
                [candidate, "/F", "/PID", str(pid)],
                env=env,
                capture_output=True,
                # taskkill 的中文提示是本机 ANSI 编码（GBK），按 UTF-8 读会**在线程里抛
                # UnicodeDecodeError**（真踩过：异常在 subprocess 的 reader 线程里，主流程只看到
                # 一个"输出是 None"）。所以显式给编码 + errors=replace，只为读日志，不影响判定。
                encoding="utf-8",
                errors="replace",
                text=True,
                timeout=STOP_WAIT_SECONDS,
            )
        except FileNotFoundError:  # pragma: no cover - PATH 里没有 taskkill
            continue
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - 罕见
            logger.warning("[Channel] taskkill 失败：%s", exc)
            return False
        if int(getattr(completed, "returncode", 1)) != 0:
            logger.warning(
                "[Channel] taskkill 未能停掉 pid=%s：%s", pid, (completed.stdout or "").strip()
            )
        return int(getattr(completed, "returncode", 1)) == 0
    return False  # pragma: no cover


def stop(
    platform: str,
    *,
    root: Path | None = None,
    killer: Callable[[int], bool] | None = None,
    wait_s: float = STOP_WAIT_SECONDS,
) -> dict[str, Any]:
    """停渠道子进程。**没在跑就如实回 `{ok: false, reason: \"not_running\"}`**，不假装成功。"""
    name = ensure_supported(platform)
    pid = live_pid(name, root=root)
    if pid is None:
        remove_pid(name, root=root)  # 陈旧 pid 文件顺手清掉，免得下次 start 误判
        return {"ok": False, "state": "stopped", "platform": name, "reason": "not_running"}

    killed = (killer or kill_pid)(pid)
    deadline = time.monotonic() + max(0.0, wait_s)
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.05)
    still_alive = pid_alive(pid)
    owned = _PROCS.pop(name, None)
    if still_alive and owned is not None and owned.poll() is None:
        owned.kill()  # 兜底：taskkill 失败时至少把自己起的那一份收掉
        still_alive = False
    if still_alive:
        return {
            "ok": False,
            "state": "running",
            "platform": name,
            "pid": pid,
            "reason": "kill_failed",
            "note": f"taskkill 没能停掉 pid={pid}（可能权限不足），可用任务管理器处理",
        }
    remove_pid(name, root=root)
    write_state(name, {"state": "stopped", "connected": False}, root)
    logger.info("[Channel] 已停 %s 子进程 pid=%s（taskkill 结果=%s）", name, pid, killed)
    return {"ok": True, "state": "stopped", "platform": name, "pid": pid, "note": "killed"}


def emit_state(ctx: Any, payload: dict[str, Any]) -> None:
    """把 `channel.state` 通知推给界面（**没有界面就静默**：CLI 与测试都不该被这条打断）。"""
    emit = getattr(ctx, "emit", None)
    if callable(emit):
        payload = {"since": now_iso(), **payload}
        emit("channel.state", payload)

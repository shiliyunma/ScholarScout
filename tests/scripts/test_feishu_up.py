"""`scripts/feishu_up.py` 的回归测试（离线，不真起进程、不真联网）。

为什么要测：这是"关机就没了"的飞书链路唯一的一键入口，它自己把三样东西（监听 / 隧道 / 自检）
串起来，任何一步静默失败都会让用户以为"跑起来了"其实没通。钉住四件真会出事的：
  A. **config 缺键** —— 不满足前提时必须说清楚缺哪个键并退出，绝不默默跑；
  B. **隧道启不了** —— 子进程起不来（命令找不到等）要报"启动失败"这一步；
  C. **URL 解析不到** —— 隧道起了但 banner 里没有公网地址，要报"解析不到"这一步；
  D. **自检失败** —— 公网地址回打 url_verification 没通过（4xx/5xx/回包不对），要报"自检失败"。
另加纯函数单测（命令可配 / URL 解析 / 回调 URL 拼接 / 前提检查）与一条 happy path。

整条主流程用注入的 `Deps` 驱动，所以本文件**零网络、零真进程**。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from scholarscout.config import RuntimeProfile

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "feishu_up.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("feishu_up", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须先登记进 sys.modules：本脚本用了 `from __future__ import annotations` + dataclass，
    # dataclasses 解析字符串注解时会去 sys.modules 找模块，不登记就 AttributeError。
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script() -> ModuleType:
    return load_script()


# ------------------------------------------------------------------ 桩件


class _FakeConfig:
    def __init__(self, runtime: RuntimeProfile) -> None:
        self.runtime = runtime


class _FakeProc:
    """够用的假进程：`poll()` 说它已退出（收尾时不再 terminate）。"""

    def __init__(self, returncode: int = 0) -> None:
        self._returncode = returncode

    def poll(self) -> int:
        return self._returncode

    def terminate(self) -> None:  # pragma: no cover - 本桩已退出，不会被调
        raise AssertionError("已退出的进程不该被 terminate")

    def wait(self, timeout: float | None = None) -> int:  # pragma: no cover
        return self._returncode

    def kill(self) -> None:  # pragma: no cover
        return


def _runtime(**over: object) -> RuntimeProfile:
    base: dict[str, object] = {
        "feishu_listener_enabled": True,
        "feishu_reply_enabled": True,
        "feishu_listener_port": 8765,
        "feishu_listener_path": "/feishu/webhook",
    }
    base.update(over)
    return RuntimeProfile(**base)  # type: ignore[arg-type]


def _make_deps(
    script: ModuleType,
    *,
    runtime: RuntimeProfile | None = None,
    listening: bool = False,
    listener: object | None = None,
    tunnel: object | None = None,
    check: tuple[bool, str] = (True, "ok"),
    calls: dict[str, object] | None = None,
):
    calls = calls if calls is not None else {}
    cfg = _FakeConfig(runtime or _runtime())

    def start_listener(port: int, path: str):
        calls["start_listener"] = (port, path)
        if listener is not None:
            return listener
        return script.ListenerHandle(address=f"http://127.0.0.1:{port}{path}", started=True)

    def start_tunnel(cmd: str, *, log_path: Path):
        calls["start_tunnel_cmd"] = cmd
        calls["start_tunnel_log"] = log_path
        if tunnel is not None:
            return tunnel
        return script.Tunnel(
            cmd=cmd, log_path=log_path, proc=_FakeProc(), url="https://ok.lhr.life"
        )

    def self_check(url: str, **_: object):
        calls["self_check_url"] = url
        return check

    deps = script.Deps(
        load_config=lambda: cfg,
        is_listening=lambda port, path: listening,
        start_listener=start_listener,
        start_tunnel=start_tunnel,
        self_check=self_check,
    )
    return deps, calls


# ================================================================== A. config 缺键


def test_missing_config_keys_exit_without_starting_anything(script: ModuleType) -> None:
    """前提没满足 → 报出缺哪个键、退出码 2、**一步都不起**（不默默跑）。"""
    deps, calls = _make_deps(
        script,
        runtime=_runtime(feishu_listener_enabled=False, feishu_reply_enabled=False),
    )
    lines: list[str] = []
    code = script.run([], deps=deps, say=lines.append, hold=False)

    assert code == 2
    assert "start_listener" not in calls and "start_tunnel_cmd" not in calls
    joined = "\n".join(lines)
    assert "feishu_listener_enabled" in joined
    assert "feishu_reply_enabled" in joined
    print("[A] 缺键退出：" + joined.splitlines()[1])


def test_port_and_path_invalid_are_reported(script: ModuleType) -> None:
    deps, _ = _make_deps(
        script, runtime=_runtime(feishu_listener_port=70000, feishu_listener_path="feishu/webhook")
    )
    lines: list[str] = []
    assert script.run([], deps=deps, say=lines.append, hold=False) == 2
    joined = "\n".join(lines)
    assert "feishu_listener_port" in joined and "feishu_listener_path" in joined


def test_allow_no_reply_downgrades_reply_gate(script: ModuleType) -> None:
    """`--allow-no-reply`：出站关着也放行（只收不发场景），但仍要求监听开着。"""
    deps, _ = _make_deps(script, runtime=_runtime(feishu_reply_enabled=False))
    assert script.run(["--allow-no-reply"], deps=deps, say=lambda _s: None, hold=False) == 0


# ================================================================== B. 隧道启不了


def test_tunnel_start_failure_reports_step(script: ModuleType) -> None:
    bad = script.Tunnel(
        cmd="ssh x",
        log_path=ROOT / "run" / "x.log",
        proc=None,
        error="隧道进程没起来：FileNotFoundError",
    )
    deps, _ = _make_deps(script, tunnel=bad)
    lines: list[str] = []
    code = script.run([], deps=deps, say=lines.append, hold=False)

    assert code == 4
    joined = "\n".join(lines)
    assert "隧道启动失败" in joined
    assert "排查" in joined


# ================================================================== C. URL 解析不到


def test_url_not_parsed_reports_step(script: ModuleType) -> None:
    bad = script.Tunnel(
        cmd="ssh x",
        log_path=ROOT / "run" / "x.log",
        proc=_FakeProc(),
        url=None,
        error="等 60s 没从隧道输出里解析到公网地址",
    )
    deps, _ = _make_deps(script, tunnel=bad)
    lines: list[str] = []
    code = script.run([], deps=deps, say=lines.append, hold=False)

    assert code == 5
    joined = "\n".join(lines)
    assert "解析不到公网地址" in joined
    assert "feishu-tunnel.log" in joined or "x.log" in joined  # 指向日志，方便排查


# ================================================================== D. 自检失败


def test_self_check_failure_reports_step(script: ModuleType) -> None:
    deps, calls = _make_deps(
        script,
        tunnel=script.Tunnel(
            cmd="ssh x",
            log_path=ROOT / "run" / "x.log",
            proc=_FakeProc(),
            url="https://abc.lhr.life",
        ),
        check=(False, '回包不对：HTTP 502，body=None（期望 200 + {"challenge": ...}）'),
    )
    lines: list[str] = []
    code = script.run([], deps=deps, say=lines.append, hold=False)

    assert code == 6
    assert calls["self_check_url"] == "https://abc.lhr.life/feishu/webhook"
    joined = "\n".join(lines)
    assert "公网自检失败" in joined
    assert "排查" in joined


# ================================================================== 成功路径


def test_happy_path_prints_callback_url_and_next_steps(script: ModuleType) -> None:
    deps, calls = _make_deps(
        script,
        tunnel=script.Tunnel(
            cmd="c", log_path=ROOT / "run" / "x.log", proc=_FakeProc(), url="https://abc.lhr.life"
        ),
    )
    lines: list[str] = []
    code = script.run([], deps=deps, say=lines.append, hold=False)

    assert code == 0
    assert calls["self_check_url"] == "https://abc.lhr.life/feishu/webhook"
    joined = "\n".join(lines)
    assert "https://abc.lhr.life/feishu/webhook" in joined
    assert "自检通过" in joined and "下一步" in joined
    print("[happy] " + joined.strip())


def test_existing_listener_is_reused_not_restarted(script: ModuleType) -> None:
    """已在监听 → 复用外部进程，绝不重复起（也不该去 stop 它）。"""
    deps, calls = _make_deps(script, listening=True)
    lines: list[str] = []
    assert script.run([], deps=deps, say=lines.append, hold=False) == 0
    assert "start_listener" not in calls
    assert "复用" in "\n".join(lines)


# ================================================================== 纯函数


def test_resolve_tunnel_cmd_default_env_and_override(script: ModuleType) -> None:
    # 默认：localhost.run + {port} 被替换
    default = script.resolve_tunnel_cmd(8765, env={})
    assert "localhost.run" in default and "8765" in default and "{port}" not in default
    # 环境变量覆盖
    env = {script.TUNNEL_CMD_ENV: "cloudflared tunnel --url http://127.0.0.1:{port}"}
    assert script.resolve_tunnel_cmd(9999, env=env).endswith("http://127.0.0.1:9999")
    # 命令行优先级最高（盖过环境变量）
    assert script.resolve_tunnel_cmd(1234, override="mycmd {port}", env=env).endswith("mycmd 1234")


def test_parse_public_url_takes_last_and_rejects_junk(script: ModuleType) -> None:
    text = "banner\nhttps://one.lhr.life ...\nreconnect\nhttps://two.lhr.life forward\n"
    assert script.parse_public_url(text) == "https://two.lhr.life"
    assert script.parse_public_url("no url here") is None
    assert script.parse_public_url("") is None


def test_build_callback_url_joins_path(script: ModuleType) -> None:
    assert script.build_callback_url("https://x.lhr.life", "/feishu/webhook") == (
        "https://x.lhr.life/feishu/webhook"
    )
    # 容忍尾部斜杠与缺前导斜杠
    assert script.build_callback_url("https://x.lhr.life/", "feishu/webhook") == (
        "https://x.lhr.life/feishu/webhook"
    )


def test_check_prerequisites_rules(script: ModuleType) -> None:
    assert script.check_prerequisites(_runtime()) == []
    assert (
        script.check_prerequisites(_runtime(feishu_reply_enabled=False), allow_no_reply=True) == []
    )
    assert script.check_prerequisites(_runtime(feishu_reply_enabled=False)) != []
    assert script.check_prerequisites(_runtime(feishu_listener_path="nope")) != []

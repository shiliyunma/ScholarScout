"""`scripts/feishu_ws_up.py`（飞书一键开机）的回归测试：**离线、无凭据、不 import SDK**。

为什么要测（旧 webhook 版 `test_feishu_up.py` 随 feishu_up.py 一起删了，这里补上等价的三条）：
1. **前提不满足时不许默默起连接**：SDK 没装 / 凭据没配 → 退出码 2 + 说清缺什么、下一步跑哪条命令；
2. **首次 import 的真实成本要先说**（本机实测 116s）：这句话漏了，第一次跑的人一定以为它挂了；
3. **飞书后台的人工步骤要列出来**（且**不得**列"填回调地址" —— 长连接没有回调，列了就是误导）。

全部依赖都注入桩，所以本用例**不需要装 lark-oapi、不联网、不碰 run/state.db**。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from scholarscout.channels import feishu

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "feishu_ws_up.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("feishu_ws_up", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script() -> ModuleType:
    return load_script()


class _FakeRuntime:
    def __init__(self, *, reply_enabled: bool = True) -> None:
        self.feishu_reply_enabled = reply_enabled


class _FakeConfig:
    def __init__(self, *, reply_enabled: bool = True) -> None:
        self.runtime = _FakeRuntime(reply_enabled=reply_enabled)


class _FakeCtx:
    """桩上下文：记录 `close()` 有没有被调（脚本自己建的上下文必须收尾）。"""

    def __init__(self, *, reply_enabled: bool = True) -> None:
        self.config = _FakeConfig(reply_enabled=reply_enabled)
        self.http = object()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    def __init__(self, *, raise_on_start: BaseException | None = None) -> None:
        self.started = 0
        self._raise = raise_on_start

    def start(self) -> None:
        self.started += 1
        if self._raise is not None:
            raise self._raise


def _creds(*, app_id: str, app_secret: str) -> feishu.FeishuCredentials:
    """造一份凭据（`can_send` 只取决于 ID/Secret 两个都非空）。"""
    return feishu.FeishuCredentials(app_id=app_id, app_secret=app_secret)


def test_missing_sdk_reports_install_command_and_exits_2(script: ModuleType) -> None:
    """SDK 缺装：**不起连接**，退出码 2，并给出 install_deps.py 这条命令。"""
    out: list[str] = []
    rc = script.run(
        say=out.append,
        sdk_probe=lambda: (False, "未安装 lark_oapi（PyPI 包名 lark-oapi）"),
        credentials_fn=lambda: _creds(app_id="a", app_secret="s"),
        client_factory=lambda *a, **k: pytest.fail("SDK 没装就不该装配客户端"),
    )
    text = "\n".join(out)
    assert rc == script.EXIT_PREREQ
    assert "install_deps.py" in text


def test_missing_credentials_lists_backstage_steps_without_callback(script: ModuleType) -> None:
    """凭据没配：列出飞书后台的人工步骤，且**不出现"填回调地址"**（长连接不需要回调）。"""
    out: list[str] = []
    rc = script.run(
        say=out.append,
        sdk_probe=lambda: (True, "lark_oapi 已安装"),
        credentials_fn=lambda: _creds(app_id="", app_secret=""),
        client_factory=lambda *a, **k: pytest.fail("没凭据就不该装配客户端"),
    )
    text = "\n".join(out)
    assert rc == script.EXIT_PREREQ
    assert "setup-feishu" in text
    assert "im.message.receive_v1" in text
    assert "回调地址" in text and "不用填" in text


def test_check_only_never_starts_the_connection(script: ModuleType) -> None:
    """`--check` 只自检：前提都满足也不起连接（退出码 0）。"""
    out: list[str] = []
    rc = script.run(
        say=out.append,
        sdk_probe=lambda: (True, "lark_oapi 已安装"),
        credentials_fn=lambda: _creds(app_id="cli_x", app_secret="sec_x"),
        client_factory=lambda *a, **k: pytest.fail("--check 不该装配客户端"),
        check_only=True,
    )
    assert rc == script.EXIT_OK
    assert any("前提自检通过" in line for line in out)


def test_happy_path_prints_import_cost_and_starts_the_client(script: ModuleType) -> None:
    """正例：先打印"首次 import 约 116 秒"，再装配并 start 一次；出站关着要明确告警。"""
    ctx = _FakeCtx(reply_enabled=False)
    client = _FakeClient()
    seen: dict[str, Any] = {}
    out: list[str] = []

    def sender_factory(http: Any, *, enabled: bool = False) -> Any:
        seen["http"], seen["enabled"] = http, enabled
        return lambda chat_id, text: {"ok": True}

    rc = script.run(
        say=out.append,
        ctx=ctx,
        sdk_probe=lambda: (True, "lark_oapi 已安装"),
        credentials_fn=lambda: _creds(app_id="cli_x", app_secret="sec_x"),
        sender_factory=sender_factory,
        client_factory=lambda *a, **k: client,
    )
    text = "\n".join(out)
    assert rc == script.EXIT_OK
    assert client.started == 1
    # 发送器被造出来时把「出站关着」如实传下去（关着 = 一次外网都不发）
    assert seen == {"http": ctx.http, "enabled": False}
    assert "出站发送未开启" in text
    # 加载成本要先说（否则第一次跑的人会以为卡死）
    assert "116" in text
    # 注入的上下文不归脚本管，不许顺手关掉别人的 ctx
    assert ctx.closed is False


def test_start_failure_is_reported_with_exit_3(script: ModuleType) -> None:
    """起连接抛异常（凭据错/出网不通）：退出码 3 + 如实打印原因，不静默退出。"""
    out: list[str] = []
    rc = script.run(
        say=out.append,
        ctx=_FakeCtx(),
        sdk_probe=lambda: (True, "lark_oapi 已安装"),
        credentials_fn=lambda: _creds(app_id="cli_x", app_secret="bad"),
        sender_factory=lambda http, *, enabled=False: lambda chat_id, text: {},
        client_factory=lambda *a, **k: _FakeClient(raise_on_start=RuntimeError("token invalid")),
    )
    text = "\n".join(out)
    assert rc == script.EXIT_START_FAILED
    assert "token invalid" in text

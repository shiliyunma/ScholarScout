"""渠道**子进程入口**（`channels/runner.py`）与 `channels/remote.py` 的测试 —— 全桩、离线、不装 SDK。

为什么值得（这是 docs/21 §4 选 B 方案后新长出来的两类风险）：

1. **子进程会变成"第二个写库者"** —— 所以钉住"子进程侧的上下文没有 db"（渠道包不许开库）；
2. **子进程的 stdout 是协议通道** —— 往那里打一行日志就等于往 JSON-RPC 里掺垃圾，
   所以断言"跑完 stdout 一个字都没有、日志全在 stderr"；
3. **出站回复必须经 RPC 走主进程**（子进程不出网）—— 断言发一条回复 = 一次 `channel.send` RPC。
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from scholarscout.channels import feishu, remote, runner, supervisor
from scholarscout.rpc.codes import KNOWN_CODES, Code

from ..conftest import redirect_home

PAYLOAD = {
    "header": {"event_type": "im.message.receive_v1"},
    "event": {
        "message": {
            "message_id": "om_test",
            "chat_id": "oc_test",
            "chat_type": "p2p",
            "message_type": "text",
            "content": json.dumps({"text": "帮我找教育智能体方向的入门资料"}),
        },
        "sender": {"sender_id": {"open_id": "ou_me"}, "sender_type": "user"},
    },
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    redirect_home(monkeypatch, tmp_path)
    for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_DM_POLICY", "FEISHU_ACK_TEXT"):
        monkeypatch.delenv(key, raising=False)
    yield
    supervisor.stop("feishu", wait_s=5.0)


class FakeStdio:
    """桩 RPC 客户端：记住每一次调用，并按方法名给回包。"""

    def __init__(self, replies: dict[str, dict] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.replies = replies or {}

    def call(self, ctx: object, method: str, params: object = None) -> dict:
        del ctx
        self.calls.append((method, dict(params) if isinstance(params, dict) else {}))
        return dict(self.replies.get(method, {}))

    def request(self, method: str, params: dict | None = None) -> dict:
        self.calls.append((method, dict(params) if params else {}))
        return dict(self.replies.get(method, {"ok": True, "message_id": "om_123456"}))

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeSdkClient:
    """桩 SDK 长连接客户端（`lark_oapi.ws.Client` 的替身）：只记参数，不连网。"""

    def __init__(self) -> None:
        self.on_reconnecting = None
        self.on_reconnected = None


class FakeWs:
    """桩长连接客户端：`build_client()` 给桩 SDK 客户端，`start()` 立刻返回。"""

    def __init__(self, ctx: object, **kwargs: object) -> None:
        self.ctx = ctx
        self.kwargs = kwargs
        self.sdk_client = FakeSdkClient()
        self.started = False
        self.says: list[str] = []

    def build_client(self) -> FakeSdkClient:
        return self.sdk_client

    def start(self, *, block: bool = True, say=print) -> FakeWs:  # type: ignore[no-untyped-def]
        self.started = True
        self.says.append(say("stub 长连接启动") or "stub")
        return self


def _reports(stdio: FakeStdio) -> list[dict]:
    """桩客户端收到的所有状态上报（`channel.report_state` 的 patch）。"""
    return [
        dict(params.get("patch") or {})
        for name, params in stdio.calls
        if name == "channel.report_state"
    ]


def _reported(stdio: FakeStdio) -> dict[str, object]:
    """把多帧上报按到达顺序合并成"子进程眼里的状态"（父进程落盘时也是这么合）。"""
    merged: dict[str, object] = {}
    for patch in _reports(stdio):
        merged.update(patch)
    return merged


def _states(stdio: FakeStdio) -> list[str]:
    """上报里出现过的 state 序列（证明状态机的**顺序**，不只看最后一帧）。"""
    return [str(patch["state"]) for patch in _reports(stdio) if "state" in patch]


def _ws_factory(holder: dict) -> object:
    def factory(ctx: object, **kwargs: object) -> FakeWs:
        ws = FakeWs(ctx, **kwargs)
        holder["ws"] = ws
        return ws

    return factory


# ------------------------------------------------------------------ 传输层（remote）


def test_stdio_client_round_trip_ignores_notifications() -> None:
    """发一行、等到自己的响应；期间推来的通知（没有 id）必须跳过，不能当成响应。"""
    lines = io.StringIO(
        '{"jsonrpc":"2.0","method":"chat.delta","params":{"text":"x"}}\n'
        '{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'
    )
    out = io.StringIO()
    client = remote.StdioClient(stdin=lines, stdout=out)
    assert client.request("channel.list", {}) == {"ok": True}
    assert json.loads(out.getvalue().strip())["method"] == "channel.list"


def test_stdio_client_maps_error_and_eof() -> None:
    error_line = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"缺参数：platform"}}\n'
    )
    client = remote.StdioClient(stdin=error_line, stdout=io.StringIO())
    with pytest.raises(remote.RemoteError) as info:
        client.request("channel.status", {})
    assert info.value.code == -32602 and "缺参数" in str(info.value)

    eof_client = remote.StdioClient(stdin=io.StringIO(""), stdout=io.StringIO())
    with pytest.raises(remote.RemoteError):
        eof_client.request("channel.list", {})


def test_stdio_client_coerces_an_unknown_code_to_the_shared_constant() -> None:
    """主进程回了表外的码 → 子进程侧只回 `rpc/codes.py` 的兜底常量（不透野码）。"""
    lines = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"error":{"code":-99999,"message":"野码"}}\n'
        '{"jsonrpc":"2.0","id":2,"error":{"message":"连码都没有"}}\n'
    )
    client = remote.StdioClient(stdin=lines, stdout=io.StringIO())
    for _ in range(2):
        with pytest.raises(remote.RemoteError) as info:
            client.request("channel.status", {})
        assert info.value.code == int(Code.NOT_CONFIGURED)
        assert info.value.code in KNOWN_CODES


def test_remote_context_has_no_database() -> None:
    """**子进程侧的上下文里根本没有 db**：想直接开库的代码会当场炸，而不是悄悄写出第二份连接。"""
    context = remote.RemoteContext()
    assert context.db is None
    assert "db" in vars(remote.RemoteContext), (
        "`db = None` 要显式写在类上，让人一眼看到「子进程不开库」"
    )
    with pytest.raises(remote.RemoteError):
        context.require_llm()


# ------------------------------------------------------------------ 前提检查


def test_unknown_platform_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    code = runner.run("wechat")
    assert code == runner.EXIT_PREREQ
    assert "只做" in capsys.readouterr().err


def test_missing_credentials_report_error_state(capsys: pytest.CaptureFixture[str]) -> None:
    """凭据没配 → 退出码 2 + **一帧 error 上报**，且子进程**一个字节都不落盘**。

    注意断言对象变了但**意图没松**：状态仍然是"error + 原因"，只是交付方式从"子进程写文件"
    改成"子进程上报、父进程落盘"（`channel.report_state`）。所以这里同时钉住两件事：
    ① 报上去的那一帧内容对；② 磁盘上没有这个文件 —— 后者才是这次修的 bug 本身
    （两个进程写同一个文件 → Windows 偶发 `os.replace PermissionError`）。
    """
    stdio = FakeStdio()
    code = runner.run("feishu", client=stdio, sdk_probe=lambda: (True, "ok"))
    assert code == runner.EXIT_PREREQ
    assert "凭据未配置" in capsys.readouterr().err
    reported = _reported(stdio)
    assert reported["state"] == "error"
    assert "凭据未配置" in str(reported["last_error"])
    assert supervisor.read_state("feishu") == {}, "子进程不许写状态文件（父进程是唯一写者）"


def test_missing_sdk_tells_the_next_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SDK 没装：退出码 2 + **说清下一步跑哪条命令**（不许只说"不行"）。"""
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_x")
    stdio = FakeStdio()
    code = runner.run(
        "feishu",
        client=stdio,
        sdk_probe=lambda: (False, "未安装 lark_oapi：先跑 `python scripts/install_deps.py`"),
    )
    assert code == runner.EXIT_PREREQ
    assert "install_deps.py" in capsys.readouterr().err
    # 状态经上报交给父进程落盘（子进程不碰文件）：error + 与 stderr 同一句原因
    reported = _reported(stdio)
    assert reported["state"] == "error"
    assert "install_deps.py" in str(reported["last_error"])
    assert supervisor.read_state("feishu") == {}


# ------------------------------------------------------------------ 起长连接（全桩）


def test_run_wires_remote_context_and_keeps_stdout_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """跑通装配 → 起连接 → 收尾；**stdout 一个字都不能有**（它是 JSON-RPC 通道）。"""
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_x")
    stdio = FakeStdio()
    holder: dict = {}
    code = runner.run(
        "feishu", client=stdio, sdk_probe=lambda: (True, "ok"), ws_factory=_ws_factory(holder)
    )
    assert code == runner.EXIT_OK
    captured = capsys.readouterr()
    assert captured.out == "", "子进程不许往 stdout 打任何东西（那是协议通道）"
    assert "长连接启动" in captured.err

    ws = holder["ws"]
    assert isinstance(ws.ctx, remote.RemoteContext)
    assert ws.ctx.db is None, "子进程侧不开库（docs/20 单写纪律）"
    assert ws.started is True
    # 状态：起连接时 starting → running，收尾后 stopped —— 且**顺序**就是这个状态机
    assert _states(stdio) == ["starting", "running", "stopped"]
    assert _reported(stdio)["pid"] == os.getpid(), "上报里带上真实子进程 pid（界面用它判定显活）"
    # 子进程只上报、不落盘：文件里没有它会话的痕迹（父进程写的是它自己那份）
    assert supervisor.read_state("feishu") == {}
    # 两个公开钩子都接上了（F4：长连接没有公开的关闭接口，但有状态钩子）
    assert callable(ws.sdk_client.on_reconnecting)
    assert callable(ws.sdk_client.on_reconnected)


def test_message_event_goes_through_rpc_and_reply_is_sent_by_the_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一条消息 → 经 RPC 跑一轮 → 回复**经 `channel.send` 由主进程发**（子进程不出网）。"""
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_x")
    monkeypatch.setenv("FEISHU_DM_POLICY", "all")
    stdio = FakeStdio(
        {
            "feishu.access": {"approved": True, "temp_open": False, "dm_policy": "all"},
            "session.ensure": {"session_id": "ses_1"},
            "chat.send": {"text": "这是主进程跑出来的回答", "stop_reason": "completed"},
        }
    )
    holder: dict = {}
    runner.run(
        "feishu", client=stdio, sdk_probe=lambda: (True, "ok"), ws_factory=_ws_factory(holder)
    )

    handler = holder["ws"].kwargs["handler"]
    decision = handler.handle_event(PAYLOAD)
    assert decision is not None and decision.action == "run"
    assert "feishu.access" in stdio.methods()
    assert "session.ensure" in stdio.methods() and "chat.send" in stdio.methods()

    sent = [params for name, params in stdio.calls if name == "channel.send"]
    assert sent, "回复必须经 channel.send 交给主进程发（子进程不自己出网）"
    # 两条都要经 channel.send：**先入站即时回执、后这一轮的回答**（docs/17 §3.11）——
    # 顺序就是子进程从"配置 → 环境变量 → 渠道核心"把 ack_text 传下去的证据。
    assert [p["text"] for p in sent] == [feishu.ack_text_from_env(), "这是主进程跑出来的回答"]
    assert sent[0]["chat_id"] == "oc_test" and sent[1]["chat_id"] == "oc_test"
    # 顺手记下"最后一条消息"（界面状态行要用）—— 同样经上报，由父进程落盘
    reported = _reported(stdio)
    assert reported["last_message_at"] and reported["last_message_from"] == "ou_me"
    assert supervisor.read_state("feishu") == {}


def test_reconnect_hooks_update_the_state_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """重连钩子 → 两帧上报；`reconnects + 1` 由**父进程**做（只加一次）。

    以前这一条是"钩子自己 read_state 再 +1 写回"—— 那正是要消掉的共享（读别人的文件）。
    现在子进程只报事实位 `reconnecting`，计数交给持有唯一快照的父进程；所以断言分两段：
    ① 子进程的帧长什么样（且帧里**没有** `reconnects`）；② 把帧真的喂给 `supervisor.report_state`
    之后，落盘结果里 `reconnects` 恰好是 1（不是 2、也没被 `reconnecting` 这种提示位污染）。
    """
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_x")
    stdio = FakeStdio()
    holder: dict = {}
    runner.run(
        "feishu", client=stdio, sdk_probe=lambda: (True, "ok"), ws_factory=_ws_factory(holder)
    )
    sdk_client = holder["ws"].sdk_client
    before = len(_reports(stdio))
    sdk_client.on_reconnecting()
    sdk_client.on_reconnected()
    frames = _reports(stdio)[before:]
    assert frames == [
        {"connected": False, "reconnecting": True, "last_error": "长连接断开，正在重连"},
        {"connected": True, "reconnecting": False, "last_error": None},
    ]
    assert not any("reconnects" in frame for frame in frames), (
        "子进程不许自己算 reconnects —— 算它就得先读那个文件，共享又回来了"
    )

    for frame in frames:
        supervisor.report_state("feishu", frame)
    state = supervisor.read_state("feishu")
    assert state["connected"] is True and state["reconnects"] == 1
    assert "reconnecting" not in state, "reconnecting 是上报侧的提示位，落盘前必须翻成计数"
    assert state["last_error"] is None


def test_build_client_failure_is_reported_and_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """装配失败（真 SDK 没装等）→ 退出码 3 + 状态 error 写明原因，不静默。"""
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_x")

    class Boom(FakeWs):
        def build_client(self) -> FakeSdkClient:
            raise RuntimeError("未安装 lark_oapi")

    def factory(ctx: object, **kwargs: object) -> FakeWs:
        return Boom(ctx, **kwargs)

    stdio = FakeStdio()
    code = runner.run("feishu", client=stdio, sdk_probe=lambda: (True, "ok"), ws_factory=factory)
    assert code == runner.EXIT_START_FAILED
    reported = _reported(stdio)
    assert reported["state"] == "error" and "lark_oapi" in str(reported["last_error"])
    assert supervisor.read_state("feishu") == {}


# ------------------------------------------------------------------ 凭据来源


def test_credentials_come_from_the_inherited_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """凭据由**继承的进程环境**拿到（不落第二份副本，docs/21 §6）。"""
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_x")
    creds = feishu.credentials_from_env()
    assert creds.can_send is True and creds.app_id == "cli_x"
    assert not list(Path(supervisor.channel_dir()).glob("*")), "读凭据不该在 run/ 下落任何东西"
    # 子进程侧**不碰** Database 这个类型：谁在渠道进程路径上 import 了它，谁就是"第二个写库者"的入口
    for module in (remote, runner, supervisor):
        assert "Database" not in vars(module), f"{module.__name__} 不许 import Database"

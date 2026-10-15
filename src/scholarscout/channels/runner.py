"""渠道子进程的入口：`python scripts/scout.py --channel feishu`（由 `channels/supervisor.py` 拉起）。

## 它干什么

1. **当 RPC 客户端**：用 `channels/remote.py` 在 stdin/stdout 上跟父进程说话（父进程是服务端）；
2. **起长连接**：装配 `channels/feishu_ws.py` 的飞书长连接，事件进来后经 RPC 交给父进程跑一轮；
3. **写状态快照**：`run/channels/<platform>.state.json`（连接 / 重连次数 / 最后一条消息 / 最后一次错误）
   —— 界面读它，父进程**不替它编**这些值；
4. **日志只走 stderr**：stdout 是协议通道，往那里打一行普通文字就等于往协议里掺垃圾。

## 三条纪律（都能在代码里指出来，不是口号）

- **不开库**：`RemoteContext.db` 就是 `None`（`channels/remote.py`），子进程要数据只能调 RPC。
- **不出网**：回复经 `channel.send` 交给父进程发（父进程有出网客户端与出站开关，一处统一）。
- **不 import SDK**：`lark_oapi` 只有真要连时才加载（首次 import 本机约 116 s），
  因此本模块的桩测**不需要装 SDK**（照 `channels/feishu_ws.py` 的同一条纪律）。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from scholarscout.channels import feishu, feishu_ws, remote, supervisor

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_PREREQ = 2
EXIT_START_FAILED = 3

#: 出站发送统一走父进程的这个 RPC（子进程不自己出网，见模块顶注）
SEND_METHOD = "channel.send"


def log(message: str) -> None:
    """子进程的日志一律走 stderr（**stdout 是 JSON-RPC 通道**）。"""
    print(f"[Channel] {message}", file=sys.stderr, flush=True)


def remote_sender(
    client: remote.StdioClient, platform: str
) -> Callable[[str, str], dict[str, Any]]:
    """把"发一条回复"变成一次 RPC（`channel.send`）。**绝不外抛**（渠道层的既有纪律）。"""

    def _send(chat_id: str, text: str) -> dict[str, Any]:
        try:
            result = client.request(
                SEND_METHOD, {"platform": platform, "chat_id": chat_id, "text": text}
            )
        except Exception as exc:
            log(f"出站发送失败（已吞）：chat_id={chat_id} err={exc}")
            return {
                "ok": False,
                "message_id": "",
                "code": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return result if isinstance(result, dict) else {"ok": False, "message_id": "", "code": 0}

    return _send


class StatefulHandler:
    """包一层 `FeishuMessageHandler`：**顺手记"最后一条消息"**，别的一字不改。

    为什么不去改 `channels/feishu.py`：那一层是"传输无关的入站核心"，加状态上报就把它和
    落盘/界面耦合起来；在这里包一层，核心仍然只做解析/拦截/跑一轮。
    """

    def __init__(
        self, inner: feishu.FeishuMessageHandler, *, platform: str, root: Path | None
    ) -> None:
        self._inner = inner
        self._platform = platform
        self._root = root

    def handle_event(self, payload: Any) -> Any:
        decision = self._inner.handle_event(payload)
        try:
            message = feishu.parse_message_event(payload if isinstance(payload, dict) else {})
        except Exception:  # pragma: no cover - 解析失败不该影响收消息
            message = None
        if message is not None:
            supervisor.write_state(
                self._platform,
                {
                    "last_message_at": supervisor.now_iso(),
                    "last_message_from": message.sender_open_id or message.chat_id,
                    "connected": True,
                },
                self._root,
            )
        return decision


def attach_connection_hooks(sdk_client: Any, *, platform: str, root: Path | None) -> None:
    """把 SDK 的两个**公开**状态钩子接到状态快照上（`docs/21 §2.1 F4`）。

    `lark_oapi.ws.Client` 只有 `start()` 是公开方法（`_disconnect` 是私有的），但它留了
    `on_reconnecting` / `on_reconnected` 两个公开钩子 —— 正好当连接健康检查的信号源。
    钩子签名在不同 SDK 版本上可能不同，所以一律 `*args` 收，**回调里绝不外抛**。
    """

    def _on_reconnecting(*args: Any) -> None:
        del args
        state = supervisor.read_state(platform, root)
        supervisor.write_state(
            platform,
            {
                "connected": False,
                "reconnects": int(state.get("reconnects") or 0) + 1,
                "last_error": "长连接断开，正在重连",
            },
            root,
        )
        log("长连接断开，正在重连…")

    def _on_reconnected(*args: Any) -> None:
        del args
        supervisor.write_state(platform, {"connected": True, "last_error": None}, root)
        log("长连接已重连")

    for name, hook in (("on_reconnecting", _on_reconnecting), ("on_reconnected", _on_reconnected)):
        try:
            setattr(sdk_client, name, hook)
        except Exception:  # pragma: no cover - SDK 用 __slots__ 时只能放弃钩子
            logger.warning("[Channel] SDK 客户端不接受 %s 钩子（跳过）", name)


def run(
    platform: str,
    *,
    root: Path | None = None,
    client: remote.StdioClient | None = None,
    sdk: feishu_ws.WsSdk | None = None,
    credentials: feishu.FeishuCredentials | None = None,
    sdk_probe: Callable[[], tuple[bool, str]] | None = None,
    ws_factory: Callable[..., feishu_ws.FeishuWsClient] | None = None,
    ack_text: str | None = None,
    say: Callable[[str], None] = log,
) -> int:
    """跑渠道子进程（**阻塞**直到长连接结束或被父进程 kill）。退出码见模块常量。"""
    name = supervisor.normalize_platform(platform)
    if name not in supervisor.PLATFORMS:  # pragma: no cover - CLI 已校验，这里兜底
        say(f"不支持的平台：{platform}（本轮只做：{'、'.join(supervisor.PLATFORMS)}）")
        return EXIT_PREREQ

    supervisor.write_state(
        name, {"state": "starting", "pid": os.getpid(), "connected": False}, root
    )
    creds = credentials or feishu.credentials_from_env()
    if not (creds.app_id and creds.app_secret):
        say("凭据未配置：先在设置里填 App ID / App Secret（或点「自动创建应用」）")
        supervisor.write_state(
            name,
            {"state": "error", "last_error": "凭据未配置（FEISHU_APP_ID / FEISHU_APP_SECRET）"},
            root,
        )
        return EXIT_PREREQ

    probe = sdk_probe or (lambda: feishu_ws.probe_sdk())
    ok, detail = probe()
    if not ok:
        say(detail)
        supervisor.write_state(name, {"state": "error", "last_error": detail}, root)
        return EXIT_PREREQ

    stdio = client or remote.StdioClient()
    ctx = remote.RemoteContext(dispatcher=stdio.call)
    sender = remote_sender(stdio, name)
    # 入站即时回执文案（docs/17 §3.11）：`ack_text` 显式给了就用它，否则读父进程注入的
    # `FEISHU_ACK_TEXT`（`supervisor.config_env` 从 `runtime.feishu_ack_text` 翻译下来），
    # 都没有才回渠道核心的缺省。**空串是合法值**（= 关掉回执），不能让 `or` 把它兜掉。
    ack = ack_text if ack_text is not None else feishu.ack_text_from_env()
    handler = StatefulHandler(
        feishu.FeishuMessageHandler(
            ctx, credentials=creds, sender=sender, dispatcher=stdio.call, ack_text=ack
        ),
        platform=name,
        root=root,
    )
    factory = ws_factory or feishu_ws.FeishuWsClient
    try:
        ws = factory(
            ctx,
            sender=sender,
            dispatcher=stdio.call,
            credentials=creds,
            handler=handler,
            sdk=sdk,
            ack_text=ack,
        )
        sdk_client = ws.build_client()
    except Exception as exc:
        message = f"装配长连接失败：{type(exc).__name__}: {exc}"
        say(message)
        supervisor.write_state(name, {"state": "error", "last_error": message}, root)
        return EXIT_START_FAILED

    attach_connection_hooks(sdk_client, platform=name, root=root)
    supervisor.write_state(
        name, {"state": "running", "connected": True, "pid": os.getpid(), "last_error": None}, root
    )
    say("长连接启动（本机主动外连，不需要公网地址 / 回调 URL / 隧道）")
    try:
        ws.start(block=True, say=say)
    except KeyboardInterrupt:  # pragma: no cover - 手工调试
        say("收到 Ctrl-C，收尾…")
    except Exception as exc:  # pragma: no cover - SDK 内部异常
        message = f"长连接退出：{type(exc).__name__}: {exc}"
        say(message)
        supervisor.write_state(name, {"state": "error", "last_error": message}, root)
        return EXIT_START_FAILED
    supervisor.write_state(name, {"state": "stopped", "connected": False}, root)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scholarscout --channel", description="渠道子进程（supervisor 拉起）"
    )
    parser.add_argument("platform", help="平台名，本轮只支持 feishu")
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    return run(args.platform)


if __name__ == "__main__":  # pragma: no cover - 由 supervisor 以脚本方式起，不直接跑
    sys.exit(main())

"""飞书一键开机：起 **websocket 长连接**（官方 SDK `lark-oapi`），**不需要隧道**。

## 为什么有这么个脚本

`docs/17 §3.8` 那套（`scripts/feishu_up.py` + 根目录 `飞书一键起飞.bat`）是 webhook 路线的：
要先起本地 `http.server` 监听、再起内网穿透隧道、再解析 banner 里的公网地址、再回打一次自检。
2026-10-14 用户拍板改走 **A 方案（websocket 长连接）** 后，这四个动作**一个都不需要了** ——
本机主动外连飞书服务器，没有公网地址、没有回调 URL、没有隧道。所以这个脚本只做三件事：

1. 前提自检（SDK 装没装 / 凭据有没有 / 出站开关开没开），**不满足就说清缺什么、下一步跑哪条命令**；
2. 装配 `channels/feishu_ws.py` 的长连接客户端（发送器由本层注入，渠道层不出网）；
3. `client.start()` **阻塞**收包（`im.message.receive_v1` → 渠道层入站核心 → 跑一轮 → 出站发送），
   Ctrl-C 收尾。

## 真实成本（别以为卡死了）

`import lark_oapi` 在本机**首次约 116 秒**（SDK 上千个子模块要写 `.pyc`），之后约 14 秒。
所以脚本会**先把这句话打出来**再加载 SDK —— 否则第一次跑的人一定会以为它挂了。
`--check` 只做前提自检（**只查规格、不 import**），几毫秒返回。

## 用法

    .venv/Scripts/python.exe scripts/feishu_ws_up.py            # 起长连接（阻塞）
    .venv/Scripts/python.exe scripts/feishu_ws_up.py --check     # 只自检，不起连接
    # 或双击项目根「飞书一键开机.bat」（自动 cd 到项目根、把 .venv/Scripts 加进 PATH、chcp 65001）

退出码：0 正常（含 Ctrl-C）/ 2 前提不满足 / 3 起连接失败。

## 还需要人工做的事（脚本代替不了）

飞书后台的操作**没有开放 API 能替你点**：建自建应用、开机器人能力、事件订阅勾
`im.message.receive_v1`。**回调地址不用填**（长连接没有回调）。清单见 `docs/17 §4.2`。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scholarscout.channels import feishu, feishu_ws  # noqa: E402
from scholarscout.fetch import feishu_send  # noqa: E402

EXIT_OK = 0
EXIT_PREREQ = 2
EXIT_START_FAILED = 3

#: 起连接前必须说清的一句话（首次 import 的真实成本；实测数字，不是估计）
FIRST_IMPORT_HINT = (
    "[Feishu] 正在加载 lark-oapi（首次 import 本机约 116 秒，之后约 14 秒；"
    "这里是同步阻塞的，属正常，别以为卡死了）"
)
#: 前提不满足时给的"下一步跑哪条命令"（两条命令都写全，别让人猜）
SETUP_HINT = "下一步：python scripts/scout.py --setup-feishu（向导会把凭据写进 .env）"
INSTALL_HINT = "下一步：python scripts/install_deps.py（按 pyproject 装，含 lark-oapi）"

Say = Callable[[str], None]

BANNER = "=== ScholarScout 飞书一键开机（websocket 长连接）==="


def _print_backstage_steps(say: Say) -> None:
    """飞书后台的四步（**只有这几步需要人工点**；回调地址那一步刻意不列，长连接不需要）。"""
    say("飞书后台（人工，脚本代替不了）：")
    say("  1. 建「自建应用」→ 拿 App ID / App Secret")
    say("  2. 开「机器人」能力")
    say(f"  3. 事件订阅勾选 {feishu.RECEIVE_EVENT}")
    say("  4. **不用填回调地址**（长连接由本机主动外连，没有公网回调）")


def run(
    *,
    say: Say = print,
    ctx: Any | None = None,
    sdk_probe: Callable[[], tuple[bool, str]] = feishu_ws.probe_sdk,
    credentials_fn: Callable[[], feishu.FeishuCredentials] = feishu.credentials_from_env,
    sender_factory: Callable[..., feishu.Sender] = feishu_send.build_sender,
    client_factory: Callable[..., feishu_ws.FeishuWsClient] = feishu_ws.build_client,
    check_only: bool = False,
) -> int:
    """跑一遍一键开机。所有外部依赖都可注入 —— 桩测能在**无凭据、无网络、无 SDK** 下跑完。"""
    say(BANNER)
    ok, note = sdk_probe()
    if not ok:
        say(f"✗ {note}")
        say(f"  {INSTALL_HINT}")
        return EXIT_PREREQ
    say(f"✓ {note}")

    credentials = credentials_fn()
    if not credentials.can_send:
        say("✗ 凭据未配置（.env 里缺 FEISHU_APP_ID / FEISHU_APP_SECRET —— 长连接要用它换 token）")
        say(f"  {SETUP_HINT}")
        _print_backstage_steps(say)
        return EXIT_PREREQ

    if check_only:
        say("✓ 前提自检通过（--check 只自检，未起连接）")
        return EXIT_OK

    own_ctx = ctx is None
    if own_ctx:
        from scholarscout.app import build_context

        ctx = build_context(db_path=None, toolsets=("research",))
    try:
        enabled = bool(ctx.config.runtime.feishu_reply_enabled)
        if not enabled:
            say(
                "！飞书出站发送未开启（runtime.feishu_reply_enabled=false，默认关）："
                "只会收到消息、跑完一轮并**记录**回复，不会真发回飞书。"
            )
        sender = sender_factory(ctx.http, enabled=enabled)
        say(FIRST_IMPORT_HINT)
        client = client_factory(ctx, sender=sender)
        try:
            client.start()
        except KeyboardInterrupt:
            say("[Feishu] 收到 Ctrl-C，长连接收尾（进程退出即断开）。")
            return EXIT_OK
        except Exception as exc:  # 起不来的原因要如实说，别静默退出
            say(f"✗ 起长连接失败：{exc!r}")
            say("  排查：凭据是否正确、网络能否出网（长连接是出站连接，本机无需开端口）。")
            return EXIT_START_FAILED
        return EXIT_OK
    finally:
        if own_ctx:
            ctx.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="feishu_ws_up",
        description="飞书一键开机：起 websocket 长连接（不需要隧道）",
    )
    parser.add_argument("--check", action="store_true", help="只做前提自检（SDK / 凭据），不起连接")
    args = parser.parse_args(argv)
    return run(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())

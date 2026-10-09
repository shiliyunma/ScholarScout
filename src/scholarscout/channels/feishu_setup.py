"""飞书接入向导（入口层，docs/18）。

对齐 Hermes `hermes gateway setup` 里 Feishu 那套体验（`interactive_setup`，adapter.py:4162）：
**编号问答 + 默认值可回车** → 打印（可选打开）创建入口 → 手工填 App ID/Secret（**只写 .env、不回显**）
→ 选连接模式 → 试拉起渠道层 → 生成配对码并提示"去飞书发给机器人" → 校验 → 写白名单 + 收回临时放行
→ 完成摘要 + 自检清单。

三处**与参照的有意差异**（docs/18 §③，都是本项目的约束逼出来的，写清不掩盖）：

1. 入口是子命令 **`scout.py --setup-feishu`**，不是"setup 菜单第 12 项"；
2. **不联网** → 不做"扫码自动建应用 / 轮询拿凭据"，只打印官网入口 + launcher 链接**模板**
   （`user_code` 用显式占位符，**绝不伪造设备码**），随后走手工填凭据
   （照 Hermes QR 失败时的回退：`QR did not complete. Continuing with manual input.`，adapter.py:4188）；
3. 配对**方向相反**：Hermes 是"机器人生成码给未知用户"（run_inbound.py:88），
   本项目是"向导生成本地码 → 用户把码发给机器人 → 渠道层捕获 openid → 向导校验批准"
   —— 这样 openid 自动落库，用户不必手抄。

所有 IO 都可注入（`ask` / `ask_secret` / `say` / `open_url` / `dispatch_fn`），
所以桩测试能在**无凭据、无网络**下把整条流程跑完。
"""

from __future__ import annotations

import getpass
import os
import re
import webbrowser
from collections.abc import Callable
from typing import Any

from scholarscout import paths, settings_io
from scholarscout.channels import feishu
from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import AppContext, dispatch

#: 从用户粘贴的文本里捞飞书 openid（`ou_` 开头 + 字母数字/下划线）。
#: 字符集**含下划线**：飞书 openid 实测形状是 `ou_` + 32 位小写字母数字（无下划线），
#: 但官方是否允许下划线**本机未核实**。含 `_` 是**更安全的一侧** —— 若真 id 带下划线而这里不含，
#: 正则会在下划线处**静默截断**，把一个错的（截短的）openid 写进白名单；宁可多认一个下划线。
#: 分组写法 `(...)+(_...)*` 顺带保证不会吃掉尾随的下划线分隔符。
#: 钉住这条行为的用例：tests/channels/test_feishu_setup.py::test_openid_with_underscore_is_not_truncated。
_OPEN_ID_RE = re.compile(r"ou_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*")

Ask = Callable[[str], str]
Say = Callable[[str], None]
OpenUrl = Callable[[str], bool]
Dispatch = Callable[..., Any]

#: 连接模式（与参照一致的两个选项）
_MODE_OPTIONS = ("webhook（本项目渠道层已实现）", "websocket（本项目渠道层尚未实现，仅记录）")
_MODE_VALUES = ("webhook", "websocket")
#: DM 授权（照参照体验的三选一，默认第 1 项）
_DM_OPTIONS = ("使用配对审批（推荐）", "放行全部私聊", "只允许白名单里的用户")
_DM_VALUES = ("pairing", "all", "allowlist")
#: 群聊（照参照体验的二选一，默认第 1 项）
_GROUP_OPTIONS = ("仅被 @ 时响应（推荐）", "关闭群聊")
_GROUP_VALUES = ("mention", "disabled")


def _default_open_url(url: str) -> bool:
    """默认的"打开链接"实现（标准库 `webbrowser`）。可注入替换，便于测试。"""
    try:
        return bool(webbrowser.open(url))
    except Exception:  # pragma: no cover - 无浏览器环境下的兜底
        return False


def _prompt_choice(
    ask: Ask, say: Say, title: str, options: tuple[str, ...], default: int = 0
) -> int:
    """编号选择；**回车走默认值**（照参照体验）。返回 0 起的下标。"""
    say(title)
    for index, option in enumerate(options, 1):
        say(f"  {index}. {option}")
    while True:
        raw = str(ask(f"选择 [{default + 1}]：") or "").strip()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        say(f"  请输入 1–{len(options)}（回车默认 {default + 1}）")


def _confirm(ask: Ask, title: str, *, default: bool = True) -> bool:
    raw = str(ask(f"{title} [{'Y/n' if default else 'y/N'}]：") or "").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes", "是", "1"}


def _write_env(values: dict[str, str]) -> None:
    """把渠道层开关/密钥写进 `.env`，并同步进程环境（自检才看得到）。"""
    settings_io.write_env(paths.home() / ".env", values)
    for key, value in values.items():
        if value:
            os.environ[key] = value


def _tick(label: str, ok: bool) -> str:
    return f"  {'✓' if ok else '·'} {label}"


def _obtain_openid(
    *, ask: Ask, say: Say, ctx: AppContext, dispatch_fn: Dispatch, code: str
) -> str | None:
    """拿到发送者的 openid。

    - **自动**（P1：渠道层收到"把码发给机器人"的私信后调 `feishu.pairing.claim`）：
      若待批里已带上 openid，直接用，不再问；
    - **手工兜底**（P0 无渠道时）：请用户把机器人回复里的 open_id 贴回来，再 claim。
    """
    pending = dispatch_fn(ctx, "feishu.pairing.list", {}).get("pending", [])
    for entry in pending:
        if entry.get("claimed") and entry.get("user_id"):
            say(f"（渠道层已自动捕获 open_id：{entry['user_id']}）")
            return str(entry["user_id"])
    raw = ask("把机器人回复里的 open_id 贴回来（形如 ou_xxx；直接粘贴整条消息也行）：")
    match = _OPEN_ID_RE.search(str(raw or ""))
    if match is None:
        return None
    open_id = match.group(0)
    claimed = dispatch_fn(ctx, "feishu.pairing.claim", {"code": code, "open_id": open_id})
    if not claimed.get("claimed"):
        say("码与机器人收到的不一致或已过期，未绑定。")
        return None
    return open_id


def run_setup(
    ctx: AppContext,
    *,
    ask: Ask = input,
    ask_secret: Ask = getpass.getpass,
    say: Say = print,
    open_url: OpenUrl = _default_open_url,
    dispatch_fn: Dispatch = dispatch,
    reconfigure: bool | None = None,
    from_: str = "scholarscout",
    tp: str = "scholarscout",
) -> int:
    """跑一遍向导。返回 0=完成，非 0=失败/取消。"""
    say("=== ScholarScout 飞书接入向导 ===")
    try:
        # ---- 0. 已配置则先确认 ----
        if (
            reconfigure is None
            and os.environ.get("FEISHU_APP_ID", "").strip()
            and not _confirm(ask, "检测到已配置飞书，是否重新配置？", default=False)
        ):
            say("保留现有配置，已退出。")
            return 0

        # ---- 1. 创建方式二选一（照参照）----
        method = _prompt_choice(
            ask,
            say,
            "如何创建机器人？",
            (
                "扫码/链接自动创建（需联网；本版打印入口与链接模板后转手工填凭据）",
                "手工填写 App ID 与 App Secret（推荐）",
            ),
            0,
        )

        # ---- 2. 域名（feishu / lark）----
        domain_idx = _prompt_choice(
            ask, say, "选择平台域名：", ("feishu（飞书·中国）", "lark（Lark·国际）"), 0
        )
        domain = "lark" if domain_idx == 1 else "feishu"

        # ---- 3. 打印（可选打开）创建入口 + launcher 链接模板 ----
        entry = feishu.open_entry_url(domain)
        say(f"飞书开放平台入口：{entry}")
        say("在后台创建应用 → 开启机器人能力 → 复制 App ID / App Secret。")
        say(
            f"（参照体验里的直建页链接形如）{feishu.launcher_url('<USER_CODE>', from_=from_, tp=tp, domain=domain)}"
        )
        say("  真 user_code 由飞书的设备码注册接口签发；本版不联网，故只打印模板、不生成设备码。")
        if _confirm(ask, "现在用浏览器打开官网入口？", default=True):
            say("  已打开浏览器。" if open_url(entry) else "  打不开就手动复制上面的链接。")
        if method == 0:
            say("（本版不联网，无法自动建应用 —— 继续走手工填凭据，与参照的 QR 失败回退一致。）")

        # ---- 4. 凭据（只写 .env，不回显）----
        app_id = str(ask("App ID：") or "").strip()
        if not app_id:
            say("已取消：没有 App ID 无法接入。")
            return 2
        app_secret = str(ask_secret("App Secret（输入不回显）：") or "").strip()
        if not app_secret:
            say("已取消：没有 App Secret 无法接入。")
            return 2

        # ---- 5. 连接模式 ----
        mode_idx = _prompt_choice(ask, say, "连接模式：", _MODE_OPTIONS, 0)
        connection_mode = _MODE_VALUES[mode_idx]
        extra: dict[str, str] = {}
        if connection_mode == "webhook":
            verification_token = str(
                ask_secret("事件订阅 Verification Token（可留空）：") or ""
            ).strip()
            encrypt_key = str(ask_secret("事件订阅 Encrypt Key（可留空）：") or "").strip()
            if verification_token:
                extra["FEISHU_VERIFICATION_TOKEN"] = verification_token
            if encrypt_key:
                extra["FEISHU_ENCRYPT_KEY"] = encrypt_key
        else:
            say("  注意：本项目渠道层目前只实现了 webhook，websocket 会被记录下来但暂不会真连。")

        _write_env(
            {
                "FEISHU_APP_ID": app_id,
                "FEISHU_APP_SECRET": app_secret,
                "FEISHU_DOMAIN": domain,
                "FEISHU_CONNECTION_MODE": connection_mode,
                **extra,
            }
        )
        say("✓ 凭据已写入 .env（App Secret 只写不读、不回显）")

        # ---- 6. 尝试拉起渠道层 ----
        listener_ok, listener_note = feishu.probe_listener(
            port=ctx.config.runtime.feishu_listener_port
        )
        say(("✓ 渠道层可拉起：" if listener_ok else "✗ 渠道层拉不起来：") + listener_note)

        # ---- 7. DM 授权三选一 ----
        dm_idx = _prompt_choice(ask, say, "私聊（DM）如何授权？", _DM_OPTIONS, 0)
        dm_policy = _DM_VALUES[dm_idx]
        dispatch_fn(ctx, "feishu.dm_policy", {"policy": dm_policy})

        if dm_policy == "pairing":
            created = dispatch_fn(ctx, "feishu.pairing.create", {})
            code = str(created["code"])
            say(
                f"去飞书私聊机器人，把这条配对码发给它（{int(created['ttl_seconds']) // 60} 分钟内有效）："
            )
            say(f"    {code}")
            open_id = _obtain_openid(ask=ask, say=say, ctx=ctx, dispatch_fn=dispatch_fn, code=code)
            if open_id is None:
                say("没有拿到 open_id —— 配对未完成。可去飞书把码发给机器人后重跑 --setup-feishu。")
                return 1
            result = dispatch_fn(ctx, "feishu.pairing.verify", {"code": code})
            say(f"✓ 配对成功：{result['user_id']} 已写入白名单；临时放行已收回")
        else:
            say(
                "已放行全部私聊（未使用配对码）。"
                if dm_policy == "all"
                else "已启用白名单模式（请稍后在设置里维护名单）。"
            )

        # ---- 8. 群聊二选一 ----
        group_idx = _prompt_choice(ask, say, "群聊如何处理？", _GROUP_OPTIONS, 0)
        group_policy = _GROUP_VALUES[group_idx]
        _write_env({"FEISHU_GROUP_POLICY": group_policy})

        # ---- 9. Home chat（可选）----
        home = str(ask("Home chat ID（可选，用于定时/通知，直接回车跳过）：") or "").strip()
        if home:
            _write_env({"FEISHU_HOME_CHANNEL": home})

        # ---- 10. 完成摘要（不打印 Secret）----
        say("")
        say("✓ 飞书接入已配置！")
        say(f"App ID: {app_id}")
        say(f"Domain: {domain}")
        say("Bot: unknown（P0 不联网探测 bot 名）")
        say(f"连接模式: {connection_mode}｜群聊: {group_policy}｜DM: {dm_policy}")
        say("配置已写入 .env；重启应用即生效。")

        # ---- 11. 自检清单 ----
        status = dispatch_fn(ctx, "feishu.status", {})
        say("")
        say("—— 自检 ——")
        say(
            _tick(
                "凭据已写入 .env（只报是否已配置）",
                bool(status["app_id_set"] and status["secret_set"]),
            )
        )
        say(
            _tick(
                f"连接模式 {status['connection_mode']}",
                status["connection_mode"] in feishu.SUPPORTED_CONNECTION_MODES,
            )
        )
        say(_tick(f"回调端口 {status['listener_port']}", listener_ok))
        if dm_policy == "pairing":
            say(_tick(f"白名单（{len(status['allowlist'])} 个身份）", bool(status["allowlist"])))
        else:
            say(
                f"  · 白名单：{len(status['allowlist'])} 个身份（当前 DM 策略 {dm_policy}，可不使用白名单）"
            )
        say(_tick("临时放行已收回", not bool(status["temp_open"])))
        return 0
    except KeyboardInterrupt:
        say("\n已取消飞书接入向导。")
        return 130
    except RpcError as exc:
        say(f"接入失败：{exc}")
        return 1

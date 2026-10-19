"""飞书渠道的**配置面**：`.env` → 值/凭据/URL（docs/17 §3.1、docs/18 §②）。

## 它在哪一层

渠道层入口里**唯一读环境变量**的地方（除回执文案，见 `feishu.py::ack_text_from_env`）。
为什么单独一层：这些函数没有状态、没有 IO（除了 `os.environ`），是"配置长什么样"的**唯一口径**
—— 连接模式只认 websocket、策略值不合法就回默认值、注册 URL 的 `from`/`tp` 可覆盖。
把它们和 handler 的编排放在一起，改一个默认值要在 900 行里翻。

## 三类

1. **接入向导常量与 URL**：`OPEN_ENTRY_URLS` / `LAUNCHER_URL_TEMPLATE` / `open_entry_url` /
   `launcher_url`（`from` / `tp` 的真实语义**未在源码/文档读到**，docs/18 §② 标 unknown —— 只当可配置参数）；
2. **策略与身份**：`connection_mode_from_env`（**只认 websocket**，配了别的值告警并回落）/
   `group_policy_from_env` / `dm_policy_from_env` / `bot_open_id_from_env` / `bot_name_from_env`；
3. **设备码注册**：`registration_from_env` / `registration_tp_from_env`。

## 与 feishu.py 的关系

从 `channels/feishu.py` 原样搬出（**只搬位置、没改函数体**）；`feishu.py` 仍重导出这里的全部名字，
`feishu.launcher_url(...)` / `feishu.connection_mode_from_env()` 等老写法照旧可用。
"""

import logging
import os

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 接入向导用到的常量与探测（docs/18）

#: 飞书开放平台入口（照 Hermes `_ONBOARD_OPEN_URLS`，adapter.py:202）
OPEN_ENTRY_URLS: dict[str, str] = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}
#: 新建机器人的 launcher 页模板。URL 与三个参数来自用户实测的终端原文
#: （`https://open.feishu.cn/page/launcher?user_code=Z2BC-3GAP&from=hermes&tp=hermes`）；
#: `from` / `tp` 的真实语义**未在源码/文档读到**（docs/18 §② 标 unknown）——只当可配置参数，不当接口契约。
LAUNCHER_URL_TEMPLATE = (
    "https://open.feishu.cn/page/launcher?user_code={user_code}&from={from_}&tp={tp}"
)

#: 设备码注册里 `from`/`tp` 的可配置来源（`docs/18 §②` 标 unknown，故做成参数而非契约）。
#: 键名与 Hermes 里固定的取值同形，进程内向导与 RPC 都读这两个环境变量。
REGISTRATION_FROM_ENV = "FEISHU_REGISTRATION_FROM"
REGISTRATION_TP_ENV = "FEISHU_REGISTRATION_TP"
DEFAULT_REGISTRATION_FROM = "scholarscout"
DEFAULT_REGISTRATION_TP = "scholarscout"

#: 支持的连接模式：**只有 websocket**（入站实现在 `feishu_ws.py`）。
#: 历史上两种都"认"，但只有 webhook 能跑；现在反过来 —— `webhook` 不再被接受，
#: 配了会**告警并回落到 websocket**（见 `connection_mode_from_env`）。
SUPPORTED_CONNECTION_MODES = ("websocket",)
DEFAULT_CONNECTION_MODE = "websocket"
#: 群聊策略：mention=仅被 @ 时响应 / disabled=关闭（对齐参照体验的二选一）
SUPPORTED_GROUP_POLICIES = ("mention", "disabled")
DEFAULT_GROUP_POLICY = "mention"
#: 私聊（DM）授权策略：pairing=配对审批（默认，未授权不跑会话）/ all=放行全部 / allowlist=仅白名单
SUPPORTED_DM_POLICIES = ("pairing", "all", "allowlist")
DEFAULT_DM_POLICY = "pairing"


def open_entry_url(domain: str = "feishu") -> str:
    """飞书 (feishu) / Lark (lark) 的开放平台入口（**真实可打开的官网入口**）。"""
    return OPEN_ENTRY_URLS.get(domain, OPEN_ENTRY_URLS["feishu"])


def launcher_url(
    user_code: str,
    *,
    from_: str = "scholarscout",
    tp: str = "scholarscout",
    domain: str = "feishu",
) -> str:
    """拼"新建机器人"的 launcher 链接。

    `user_code` 由飞书的设备码注册接口签发（Hermes: `_begin_registration` 的
    `verification_uri_complete`，adapter.py:3937）。P0 不联网拿不到真码，调用方传
    占位符即可 —— **绝不伪造设备码**。
    """
    url = LAUNCHER_URL_TEMPLATE.format(user_code=user_code, from_=from_, tp=tp)
    return url.replace("open.feishu.cn", "open.larksuite.com") if domain == "lark" else url


def connection_mode_from_env() -> str:
    """渠道层的连接模式（读 `.env` 的 `FEISHU_CONNECTION_MODE`，键名照 Hermes adapter.py:1311）。

    **只认 websocket**（唯一实现）。配了别的值（含历史遗留的 `webhook`）一律回落到 websocket 并**告警**：
    静默按另一种模式跑起来，比当场说清楚更难查 —— 那正是"以为在收事件、其实一个都收不到"的来源。
    """
    raw = (os.environ.get("FEISHU_CONNECTION_MODE") or DEFAULT_CONNECTION_MODE).strip().lower()
    if raw in SUPPORTED_CONNECTION_MODES:
        return raw
    logger.warning(
        "[Feishu] FEISHU_CONNECTION_MODE=%s 不受支持（本项目只实现 websocket 长连接），按 websocket 处理",
        raw,
    )
    return DEFAULT_CONNECTION_MODE


def group_policy_from_env() -> str:
    """渠道层的群聊策略（读 `.env` 的 `FEISHU_GROUP_POLICY`）。"""
    policy = (os.environ.get("FEISHU_GROUP_POLICY") or DEFAULT_GROUP_POLICY).strip().lower()
    return policy if policy in SUPPORTED_GROUP_POLICIES else DEFAULT_GROUP_POLICY


def dm_policy_from_env() -> str:
    """渠道层的私聊策略（读 `.env` 的 `FEISHU_DM_POLICY`；与 `rpc/feishu.py` 同名键）。"""
    policy = (os.environ.get("FEISHU_DM_POLICY") or DEFAULT_DM_POLICY).strip().lower()
    return policy if policy in SUPPORTED_DM_POLICIES else DEFAULT_DM_POLICY


def bot_open_id_from_env() -> str:
    """机器人自身 open_id（键名照 Hermes `FEISHU_BOT_OPEN_ID`，adapter.py:1316）。"""
    return os.environ.get("FEISHU_BOT_OPEN_ID", "").strip()


def bot_name_from_env() -> str:
    """机器人名字（键名照 Hermes `FEISHU_BOT_NAME`，adapter.py:1318）。"""
    return os.environ.get("FEISHU_BOT_NAME", "").strip()


def registration_from_env() -> str:
    """设备码注册 URL 里的 `from`（默认 `scholarscout`，可用 `FEISHU_REGISTRATION_FROM` 覆盖）。"""
    return (
        os.environ.get(REGISTRATION_FROM_ENV) or DEFAULT_REGISTRATION_FROM
    ).strip() or DEFAULT_REGISTRATION_FROM


def registration_tp_from_env() -> str:
    """设备码注册 URL 里的 `tp`（默认 `scholarscout`，可用 `FEISHU_REGISTRATION_TP` 覆盖）。"""
    return (
        os.environ.get(REGISTRATION_TP_ENV) or DEFAULT_REGISTRATION_TP
    ).strip() or DEFAULT_REGISTRATION_TP

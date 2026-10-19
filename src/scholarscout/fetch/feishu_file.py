"""飞书**文件**出站：上传本地文件换 `file_key` → 私聊发 `msg_type=file`（docs/17 §3.7 / §3.12）。

## 这一层为什么单独一个文件

拆自 `fetch/feishu_send.py`（原 782 行，超 600 的硬上限）。文件发送是一条**独立能力**：
自己的结果类型 :class:`FileOutcome`、自己的 30 MB / 空文件本地校验、自己的错误码表，
与"发一条文本"没有共享状态 —— 唯一交叉是下面这些**纯函数 / 常量基元**。

## 为什么协议常量与错误码人话也在这里

:data:`SEND_MESSAGE_PATH` / :func:`send_message_url` / :data:`ERROR_HINTS` /
:func:`explain_code` 都是**纯的、零 IO**，文本路径与文件路径都要用。它们随本模块下沉，
于是本模块成为 `fetch/` 里的**底层出站模块**：只 import `fetch/http.py` 与
`fetch/feishu_onboard.py`，**不 import `rpc`**。方向单一
（`feishu_send` → `feishu_file`），没有循环 import。

## 发送器工厂不在这里

`build_file_sender` / `_no_network_file_sender` 留在 `feishu_send.py`：它们必须按
`feishu_send` 的**模块全局名**去调 :func:`send_file`
（`tests/fetch/test_feishu_file_send.py` 用 `monkeypatch.setattr(feishu_send, "send_file", ...)`
打桩）。`feishu_send` 因此把 :func:`send_file` 重导出成自己的模块属性。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scholarscout.fetch.feishu_onboard import ONBOARD_OPEN_URLS, fetch_tenant_token
from scholarscout.fetch.http import FetchError, HttpClient

#: 发送消息路径（出处见模块顶注：SDK `create_message_request.py:25`）
SEND_MESSAGE_PATH = "/open-apis/im/v1/messages"
#: 上传文件路径（出处：飞书官方「上传文件」`POST /open-apis/im/v1/files`；
#: SDK `create_file_request.py::create_file_request.uri` 同值）
UPLOAD_FILE_PATH = "/open-apis/im/v1/files"
#: 官方上限：单文件 30 MB，且不允许空文件（「上传文件 → 使用限制」原文：
#: 「文件大小不得超过 30 MB，且不允许上传空文件」；错误码 234006 / 234010）
MAX_FILE_BYTES = 30 * 1024 * 1024
#: 官方 `file_type` 枚举；**不在枚举里的（.md/.txt/.csv/.docx…）一律 `stream`**
#: （官方原文：「若上传文件不属于以上枚举类型，可以使用 stream 格式」）。
#: 这里只映射确定属于枚举的后缀：把 .docx 报成 `doc` 是猜的，猜错会被 230055 拒。
FILE_TYPE_ENUM: tuple[str, ...] = ("opus", "mp4", "pdf", "doc", "xls", "ppt", "stream")
_FILE_TYPE_BY_SUFFIX: dict[str, str] = {
    ".opus": "opus",
    ".mp4": "mp4",
    ".pdf": "pdf",
    ".doc": "doc",
    ".xls": "xls",
    ".ppt": "ppt",
}
#: `receive_id_type`：文本走 `chat_id`（群/会话），**文件只能按 `open_id` 私聊发**
RECEIVE_ID_TYPE_CHAT = "chat_id"
RECEIVE_ID_TYPE_OPEN_ID = "open_id"
#: 文本消息类型 / 文件消息类型
MSG_TYPE_TEXT = "text"
MSG_TYPE_FILE = "file"
#: 默认平台域名（feishu / lark，两套基址在 `fetch/feishu_onboard.py::ONBOARD_OPEN_URLS`）
DEFAULT_DOMAIN = "feishu"


def send_message_endpoint(domain: str = DEFAULT_DOMAIN) -> str:
    """发消息端点（开放平台基址 + `SEND_MESSAGE_PATH`）。未知域名回退 feishu（不抛）。"""
    base = ONBOARD_OPEN_URLS.get(domain, ONBOARD_OPEN_URLS["feishu"])
    return f"{base}{SEND_MESSAGE_PATH}"


def send_message_url(
    domain: str = DEFAULT_DOMAIN, *, receive_id_type: str = RECEIVE_ID_TYPE_CHAT
) -> str:
    """带 query 的完整发送 URL（`post_json` 只认 URL 本身带的 query）。"""
    return f"{send_message_endpoint(domain)}?receive_id_type={receive_id_type}"


# ------------------------------------------------ 发文件（上传拿 file_key → 私聊发送）
#
# 官方口径（逐字来自 open.feishu.cn 的 markdown 版文档，抓取日期 2026-10-18）：
#
# | 项 | 值 | 出处 |
# |---|---|---|
# | 上传端点 | `POST /open-apis/im/v1/files`（multipart/form-data） | 「上传文件」 |
# | 上传字段 | `file_type` / `file_name`（带后缀）/ `file`（可选 `duration`） | 同上 |
# | 单文件上限 | **30 MB**，且**不允许空文件** | 同上「使用限制」 |
# | 上传权限 | `im:resource` 或 `im:resource:upload`（**勾一个即可**） | 同上 |
# | 发送端点 | `POST /open-apis/im/v1/messages?receive_id_type=open_id` | 「发送消息」 |
# | 文件消息体 | `msg_type=file`，`content` 是 JSON **字符串** `{"file_key": "..."}` | 「发送消息内容 → 文件 file」 |
# | 发送权限 | `im:message` / `im:message:send_as_bot` / `im:message:send`（勾一个即可） | 「发送消息」 |
# | 文本消息体上限 | 150 KB（超了是 `230025`） | 同上 `content` 字段说明 |
#
# **为什么文件只按 `open_id` 发、形参里没有 chat_id**：项目口径是"只私聊、不发群"
# （`docs/21 §2.1` 的 `dm_only`）。把接收者钉成 `open_id` 之后，**结构上就发不了群**
# —— 群聊的 id 是 `oc_...`，传进来只会被平台判 `230034 receive_id 无效`，而不是"悄悄发到群里"。

#: 平台错误码 → 人话（**权限没勾时要回可读的话，不是抛栈**）。
#: 错误码出处：「上传文件」「发送消息」两页的错误码表。
ERROR_HINTS: dict[int, str] = {
    99992361: "open_id 跨应用（飞书 open_id 按应用隔离）：这个 id 不是本应用发出来的，"
    "让对方把配对码私聊给机器人重新配对（或 feishu.pairing.revoke 删掉这条再重配）",
    99991672: "应用缺少权限：去飞书后台给应用勾上 im:resource（上传文件）"
    "与 im:message:send_as_bot（发消息），发布后重试",
    230027: "应用缺少发消息权限（im:message / im:message:send_as_bot）",
    230006: "应用没启用机器人能力（飞书后台 → 应用能力 → 机器人）",
    234007: "应用没启用机器人能力（飞书后台 → 应用能力 → 机器人）",
    230013: "这个用户不在机器人的可用范围内（后台 → 版本管理与发布 → 可用范围）",
    230034: "接收者 id 无效：文件只按 open_id 私聊发，群 id（oc_…）不行",
    230017: "机器人只能发送自己上传的文件（file_key 不是本应用上传的）",
    230055: "上传时的 file_type 与消息类型不匹配（pdf/doc/xls/ppt/stream → msg_type=file）",
    234006: "文件超过 30 MB（图片上限是 10 MB）",
    234010: "不允许上传 0 字节的空文件",
}


def explain_code(code: int, msg: Any = "") -> str:
    """把平台错误码翻成一句人话（认不出来的只说原始码与原文，**不编原因**）。"""
    hint = ERROR_HINTS.get(int(code))
    raw = f"飞书返回 code={code}：{msg}" if msg else f"飞书返回 code={code}"
    return f"{raw}｜{hint}" if hint else raw


@dataclass(frozen=True)
class FileOutcome:
    """一次"发文件"的结果（上传 + 发送两步合成一个出口）。"""

    ok: bool
    message_id: str = ""
    file_key: str = ""
    file_name: str = ""
    size_bytes: int = 0
    code: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message_id": self.message_id,
            "file_key": self.file_key,
            "file_name": self.file_name,
            "size_bytes": self.size_bytes,
            "code": self.code,
            "error": self.error,
        }


def upload_file_endpoint(domain: str = DEFAULT_DOMAIN) -> str:
    """上传端点（开放平台基址 + `UPLOAD_FILE_PATH`）。未知域名回退 feishu（不抛）。"""
    base = ONBOARD_OPEN_URLS.get(domain, ONBOARD_OPEN_URLS["feishu"])
    return f"{base}{UPLOAD_FILE_PATH}"


def file_type_for(name: str) -> str:
    """按后缀映射官方 `file_type`；**认不出来一律 `stream`**（官方允许的兜底档）。"""
    suffix = Path(name).suffix.lower()
    return _FILE_TYPE_BY_SUFFIX.get(suffix, "stream")


def _check_file(path: Path) -> tuple[int, str]:
    """返回 `(字节数, 错误)`；**空 = 通过**。超限/空文件在这里就拒，不去打接口。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return 0, f"读不到文件：{path}（{exc}）"
    if size <= 0:
        return 0, f"文件是空的（飞书不允许 0 字节文件）：{path.name}"
    if size > MAX_FILE_BYTES:
        return size, (
            f"文件 {size / 1024 / 1024:.1f} MB 超过飞书上限 30 MB：{path.name}"
            "（官方「上传文件 → 使用限制」；大文件请先在本地压缩或拆分）"
        )
    return size, ""


def _upload(http: HttpClient, *, token: str, path: Path, domain: str) -> tuple[str, int, str]:
    """真正打上传接口。返回 `(file_key, code, error)`（**不抛**）。"""
    try:
        payload = path.read_bytes()
    except OSError as exc:
        return "", 0, f"读不到文件：{path}（{exc}）"
    try:
        result = http.post_multipart(
            upload_file_endpoint(domain),
            fields={"file_type": file_type_for(path.name), "file_name": path.name},
            files={"file": (path.name, payload, "application/octet-stream")},
            headers={"Authorization": f"Bearer {token}"},
        )
    except FetchError as exc:
        return "", 0, f"上传请求失败（{exc.kind}）：{exc}"
    try:
        body = json.loads(result.text)
    except (json.JSONDecodeError, ValueError):
        return "", 0, f"上传接口返回的不是 JSON：{result.text[:200]!r}"
    if not isinstance(body, Mapping):
        return "", 0, "上传接口返回的不是 JSON 对象"
    code = int(body.get("code") or 0)
    if code != 0:
        return "", code, explain_code(code, body.get("msg"))
    data = body.get("data")
    file_key = str(data.get("file_key") or "") if isinstance(data, Mapping) else ""
    if not file_key:
        return "", 0, "上传接口没回 file_key（回包形状变了？）"
    return file_key, 0, ""


def _send_uploaded(
    http: HttpClient,
    *,
    token: str,
    open_id: str,
    file_key: str,
    file_name: str,
    size: int,
    domain: str,
) -> FileOutcome:
    """用 `file_key` 发一条 `msg_type=file`（`receive_id_type=open_id`，**结构上发不了群**）。"""
    body: dict[str, Any] = {
        "receive_id": open_id,
        "msg_type": MSG_TYPE_FILE,
        "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
        "uuid": uuid.uuid4().hex,
    }
    try:
        result = http.post_json(
            send_message_url(domain, receive_id_type=RECEIVE_ID_TYPE_OPEN_ID),
            body,
            accept="application/json",
            cache=False,
            headers={"Authorization": f"Bearer {token}"},
        )
    except FetchError as exc:
        return FileOutcome(
            ok=False,
            file_key=file_key,
            file_name=file_name,
            size_bytes=size,
            error=f"发送请求失败（{exc.kind}）：{exc}",
        )
    try:
        payload = json.loads(result.text)
    except (json.JSONDecodeError, ValueError):
        return FileOutcome(
            ok=False,
            file_key=file_key,
            file_name=file_name,
            size_bytes=size,
            error=f"发送接口返回的不是 JSON：{result.text[:200]!r}",
        )
    if not isinstance(payload, Mapping):
        return FileOutcome(
            ok=False,
            file_key=file_key,
            file_name=file_name,
            size_bytes=size,
            error="发送接口返回的不是 JSON 对象",
        )
    code = int(payload.get("code") or 0)
    if code != 0:
        return FileOutcome(
            ok=False,
            file_key=file_key,
            file_name=file_name,
            size_bytes=size,
            code=code,
            error=explain_code(code, payload.get("msg")),
        )
    data = payload.get("data")
    message_id = str(data.get("message_id") or "") if isinstance(data, Mapping) else ""
    return FileOutcome(
        ok=True,
        message_id=message_id,
        file_key=file_key,
        file_name=file_name,
        size_bytes=size,
    )


def send_file(
    http: HttpClient,
    *,
    app_id: str,
    app_secret: str,
    open_id: str,
    path: str | Path,
    domain: str = DEFAULT_DOMAIN,
) -> FileOutcome:
    """上传一个本地文件并**私聊**发给 `open_id`。**失败一律 `ok=False` + 原因，不抛。**

    校验顺序刻意放在"打接口之前"：缺凭据 / 缺 open_id / 文件不存在 / 空文件 / 超 30 MB
    都在本地拒掉 —— 这些原因用户自己能修，不需要先吃一个平台错误码。
    """
    if not (app_id and app_secret):
        return FileOutcome(ok=False, error="缺凭据：FEISHU_APP_ID / FEISHU_APP_SECRET 未配置")
    if not (open_id or "").strip():
        return FileOutcome(ok=False, error="缺 open_id：文件只能私聊发给具体的人")
    target = Path(path)
    size, bad = _check_file(target)
    if bad:
        return FileOutcome(ok=False, file_name=target.name, size_bytes=size, error=bad)

    token = fetch_tenant_token(http, app_id=app_id, app_secret=app_secret, domain=domain)
    if token is None:
        return FileOutcome(
            ok=False,
            file_name=target.name,
            size_bytes=size,
            error="取 tenant_access_token 失败（凭据不对或网络错误）",
        )
    file_key, code, error = _upload(http, token=token.token, path=target, domain=domain)
    if not file_key:
        return FileOutcome(ok=False, file_name=target.name, size_bytes=size, code=code, error=error)
    return _send_uploaded(
        http,
        token=token.token,
        open_id=open_id.strip(),
        file_key=file_key,
        file_name=target.name,
        size=size,
        domain=domain,
    )

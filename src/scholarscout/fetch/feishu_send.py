"""飞书**出站发送器**：把一轮回答推回飞书（docs/17 §3.7）。

## 它在哪一层 / 为什么单独一个文件

这是**出网**能力，只能落在 `fetch/`（`docs/03 §2`：出网只走 `fetch/http.py`）。
本模块**不直接 import httpx**，所有请求都经项目的唯一出网口 `HttpClient`
（限速 / 退避 / UA / 超时 / 体积上限全在那一层生效）。

**渠道层（`channels/`）不许 import `fetch`**（`tests/test_layering.py::RULES["channels"]`），
所以这里提供的是**渠道层形状**的发送器工厂 :func:`build_sender`（`sender(chat_id, text) -> dict`）；
由**入口层**（`scripts/feishu_ws_up.py` 起长连接时）把它注入 `channels/feishu_ws.py` 的 `FeishuWsClient`，
再随之下沉到入站核 `channels/feishu.py::FeishuMessageHandler`。渠道层只负责"一轮跑完调 `self._sender(...)`"
这一处，不关心它是不是真出网。

## 接口契约（逐条带出处，不臆造）

发消息端点 / 字段（**实测来源**：本机 lark-oapi SDK 与本机 Hermes 适配器）：

- **端点** `POST {open_base}/open-apis/im/v1/messages`
  （SDK `lark_oapi/api/im/v1/model/create_message_request.py:25`：
  `create_message_request.uri = "/open-apis/im/v1/messages"`；HTTP 方法见同文件
  `create_message_request.http_method = HttpMethod.POST`；开放平台基址同
  `fetch/feishu_onboard.py::ONBOARD_OPEN_URLS`）。
- **query** `receive_id_type=chat_id`（SDK `create_message_request.py` 的
  `receive_id_type(...)` 把该值塞进 query；Hermes 亦按 `chat_id` 判定
  `"open_id" if chat_id.startswith("ou_") else "chat_id"`，`plugins/platforms/feishu/adapter.py:3602`）。
- **body**（JSON 对象）：`receive_id` / `msg_type` / `content` / `uuid`
  （SDK `create_message_request_body.py:8-12` 的 `_types`）。其中
  - `msg_type` 取 `"text"`；
  - **`content` 是一个 JSON 字符串**（不是对象）：`"{\"text\": ...}"`（本模块
    :func:`text_content`）；解析侧见 `channels/feishu.py::_text_from_content`；
  - `uuid` 是**幂等键**（SDK 同字段）：带上它，飞书对同一 uuid 去重，重试也不会重复投递。
- **鉴权** 头 `Authorization: Bearer <tenant_access_token>`
  （SDK `create_message_request.token_types = {TENANT, USER}`；Hermes 同一端点
  `self._client.im.v1.message.create`，`adapter.py:3607`）。token **复用**
  `fetch/feishu_onboard.py::fetch_tenant_token`（`POST /open-apis/auth/v3/tenant_access_token/internal`，
  已实测），**不重写**。
- **成功回包**：`{"code": 0, "msg": "success", "data": {"message_id": "om_..."}}`。
  Hermes 取 `response.data.message_id`（`adapter.py:3614-3616 / :3631`）；本模块按同形状解析。

## 两条工程口径（写清，不掩盖）

1. **失败明确、绝不抛**：取 token 失败 / 网络错误 / `code != 0` 都回 :class:`SendOutcome`
   （`ok=False` + `error`），**不抛异常** —— 渠道层的发送发生在"一轮跑完之后"，
   一次发送失败不该把会话或回调处理搞崩（`channels/feishu.py::handle` 也会兜一层）。
2. **发送不缓存**：`post_json(..., cache=False)`。发送是**写操作**，缓存会把"这次没发出去"
   钉死成"发过了"；而且同一段文本要能重复发（用户重问同一句）。

## 拆分口径（2026-10-19）

文件发送（上传 `file_key` → `msg_type=file`）已整块搬到 `fetch/feishu_file.py`：
那里还放着协议常量与错误码人话（`SEND_MESSAGE_PATH` / `send_message_url` / `explain_code`），
本模块从它 import 并**重导出**（见下方 `__all__`），所以 `from ...feishu_send import send_file`
这类老写法一个都不破。方向单一：`feishu_send` → `feishu_file`，没有循环 import。
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scholarscout.fetch.feishu_file import (
    _FILE_TYPE_BY_SUFFIX,
    DEFAULT_DOMAIN,
    ERROR_HINTS,
    FILE_TYPE_ENUM,
    MAX_FILE_BYTES,
    MSG_TYPE_FILE,
    MSG_TYPE_TEXT,
    RECEIVE_ID_TYPE_CHAT,
    RECEIVE_ID_TYPE_OPEN_ID,
    SEND_MESSAGE_PATH,
    UPLOAD_FILE_PATH,
    FileOutcome,
    explain_code,
    file_type_for,
    send_file,
    send_message_endpoint,
    send_message_url,
    upload_file_endpoint,
)
from scholarscout.fetch.feishu_onboard import fetch_tenant_token
from scholarscout.fetch.http import FetchError, HttpClient

#: 公开面（含**向后兼容的重导出**）：拆分前这些名字都住在本模块，测试与调用点按
#: `from scholarscout.fetch.feishu_send import X` 取用，所以一个都不许消失。
#: 文件发送的实现体在 `fetch/feishu_file.py`，这里只做属性转发（`send_file` 被
#: `monkeypatch.setattr(feishu_send, "send_file", ...)` 打桩，工厂必须按本模块全局名查它）。
__all__ = [
    "CODE_CLOSE",
    "CODE_OPEN",
    "DEFAULT_DOMAIN",
    "ERROR_HINTS",
    "FILE_TYPE_ENUM",
    "MAX_FILE_BYTES",
    "MSG_TYPE_FILE",
    "MSG_TYPE_TEXT",
    "RECEIVE_ID_TYPE_CHAT",
    "RECEIVE_ID_TYPE_OPEN_ID",
    "SEND_MESSAGE_PATH",
    "UPLOAD_FILE_PATH",
    "_FILE_TYPE_BY_SUFFIX",  # 测试按属性读它（tests/fetch/test_feishu_file_send.py:149）
    "FileOutcome",
    "SendOutcome",
    "build_file_sender",
    "build_private_sender",
    "build_sender",
    "credentials_from_env",
    "explain_code",
    "file_type_for",
    "send_file",
    "send_message_endpoint",
    "send_message_url",
    "send_text",
    "text_content",
    "to_feishu_text",
    "upload_file_endpoint",
]

logger = logging.getLogger(__name__)

#: 凭据环境变量名（键名照 Hermes `plugin.yaml:13-23` 与 `channels/feishu.py::credentials_from_env`）
APP_ID_ENV = "FEISHU_APP_ID"
APP_SECRET_ENV = "FEISHU_APP_SECRET"


@dataclass(frozen=True)
class SendOutcome:
    """一次发送的结果。`ok` 为真时 `message_id` 是飞书返回的消息 id。"""

    ok: bool
    message_id: str = ""
    code: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        """转成渠道层 `Sender` 约定的 dict 形状。"""
        return {
            "ok": self.ok,
            "message_id": self.message_id,
            "code": self.code,
            "error": self.error,
        }


def text_content(text: str) -> str:
    """飞书 `content` 字段是**一个 JSON 字符串**（`{"text": "..."}`），不是 JSON 对象。"""
    return json.dumps({"text": text}, ensure_ascii=False)


# ---------------------------------------------------------------- 纯文字化（用户拍板：飞书不收 md）
#
# 为什么要有这一层：飞书 `text` 消息**不解析 markdown**，`**粗体**`、`## 标题`、`| 表 |`
# 会原样显示成一堆符号，比纯文字更难读。所以所有出站文本（回答 / 回执 / 配对提示 /
# 文件回执）统一过 :func:`to_feishu_text` 剥掉标记，**但代码块内容与换行一个字节都不动**
# —— 代码里的 `#`/`*`/`-` 是代码本身，剥掉就把程序改坏了。

_FENCE_RE = re.compile(r"^\s{0,3}(?:```|~~~)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}[ \t]+")
#: 引用：**一次把整串 `>` 剥干净**（`>>` 只剥一层会剩 `> x`，第二次调用再剥一次 → 不幂等）
_QUOTE_RE = re.compile(r"^\s{0,3}(?:>[ \t]?)+")
_BULLET_RE = re.compile(r"^(\s*)[-*+][ \t]+")
_HR_RE = re.compile(r"^\s{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_LINK_RE = re.compile(r"!?\[([^\]\n]*)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_BOLD_UNDER_RE = re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?=\S)([^*\n]+?)(?<=\S)\*(?!\*)")
_ITALIC_UNDER_RE = re.compile(r"(?<!\w)_(?=\S)([^_\n]+?)(?<=\S)_(?!\w)")
#: 行内代码：**反引号要按"整串"配对**（``x`` 也有、```x``` 也有）。
#: 只认单个反引号时 ``x`` 会被剥成 `` `x` `` —— 又变出一个能被再剥的对象（不幂等）。
_CODE_SPAN_RE = re.compile(r"(`+)([^`\n]+?)\1")

# ---------------------------------------------------------------- 代码区边界标记
#
# **为什么输出里要留标记**（这是"幂等"的真因，不是装饰）：
# 围栏（```）本身是"这段是代码，别动"的唯一凭据。原实现把围栏行删掉、代码内容原样留下
# → 输出里"代码已经裸露、保护却没了"：第二次调用会把 `# 注释` 当标题剥掉、把 `| a | b |`
# 当表格拆开、把 `**x**` 当粗体剥掉（实测 30 例里 8 例如此）。这不可能靠"多剥一次"修好。
# 所以 f 的输出必须**自带边界**：代码区用这两行夹住，f 第二次见到它们就原样保留区内容。
# 选箱线字符（不是 markdown 的 `---`/`***`）：`_HR_RE` 不认它，飞书里显示成一条分隔线。
CODE_OPEN = "┌──── 代码 ────"
CODE_CLOSE = "└─────────────"


def _link_text(match: re.Match[str]) -> str:
    """`[标题](url)` → `标题 url`（纯文字里链接只剩文字就点不开了，所以把地址留着）。"""
    label, url = match.group(1).strip(), match.group(2).strip()
    return f"{label} {url}".strip() if label else url


def _plain_line(line: str) -> str:
    """一行正文的 markdown 剥离（**代码块里的行走不到这里**）。"""
    if not line.strip():
        return ""
    if _HR_RE.match(line) or _TABLE_SEP_RE.match(line):
        return ""  # 水平线 / 表格的分隔行（|---|---|）在纯文字里没有意义
    text = line
    if _TABLE_ROW_RE.match(text) or text.count("|") >= 2:
        # 表格：去掉管道，用两个空格保留列间距（一行仍然是同一行，不重排结构）
        cells = [cell.strip() for cell in text.strip().strip("|").split("|")]
        text = "  ".join(cell for cell in cells if cell)
    text = _HEADING_RE.sub("", text)
    text = _QUOTE_RE.sub("", text)
    text = _BULLET_RE.sub(r"\1· ", text)  # 无序列表 `-` / `*` → `·`；有序 `1.` 保留
    text = _LINK_RE.sub(_link_text, text)
    text = _BOLD_RE.sub(r"\1", text)
    text = _BOLD_UNDER_RE.sub(r"\1", text)
    text = _ITALIC_RE.sub(r"\1", text)
    text = _ITALIC_UNDER_RE.sub(r"\1", text)
    text = _CODE_SPAN_RE.sub(r"\2", text)  # 第 1 组是反引号串，第 2 组才是内容
    return text.rstrip()


def to_feishu_text(text: str) -> str:
    """把 markdown 剥成飞书能正常显示的**纯文字**（纯函数、**幂等**：`f(f(x)) == f(x)`）。

    规则（可逐条断言，见 `tests/fetch/test_feishu_text.py`）：

    - 去标题 `#`、粗体 `**`/`__`、斜体 `*`/`_`、行内代码反引号、引用 `>`（**整串 `>>` 一次剥净**）；
    - 代码围栏（` ``` ` 那两行）换成 :data:`CODE_OPEN` / :data:`CODE_CLOSE` 两行标记，
      **围栏里的内容一个字节都不动**（含缩进、`#`、`*`、`|`、空行）；
    - 表格去管道（单元格用两个空格分隔），表格分隔行与水平线整行去掉；
    - 无序列表 `-`/`*`/`+` → `·`；有序列表 `1.` 保留（飞书里本来就看得懂）；
    - 换行全部保留；正文里连续空行压成一个空行（飞书里连续空行只是多几行白），代码区内的空行不动。

    **幂等为什么必须成立**（这是修过的真 bug）：回执 / 文件回执这类文本会经
    `send_text` → `to_feishu_text` 再走一遍；输出若不是 f 的不动点，第二次调用就继续剥。
    原实现的真因是「围栏行删掉、代码内容原样留着」：保护（围栏）随标记一起消失，
    第二次调用就把 `# 注释` 当标题、把 `| a | b |` 当表格、把 `**x**` 当粗体继续剥。
    所以现在代码区的边界**跟着输出走**（见 :data:`CODE_OPEN`），f 第二次见到它就不再动里面的行。
    """
    if not text:
        return ""
    source = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[tuple[bool, str]] = []  # (是否代码区, 行)
    in_code = False
    for line in source.split("\n"):
        stripped = line.strip()
        if in_code:
            if stripped == CODE_CLOSE or _FENCE_RE.match(line):
                in_code = False  # 收尾统一成同一个标记（输入用了 ``` 还是标记都一样）
                out.append((False, CODE_CLOSE))
                continue
            out.append((True, line))  # 代码内容原样
            continue
        if stripped == CODE_OPEN or _FENCE_RE.match(line):
            in_code = True
            out.append((False, CODE_OPEN))
            continue
        out.append((False, _plain_line(line)))
    if in_code:
        out.append((False, CODE_CLOSE))  # 围栏没闭合也要收口，否则下一次调用会把代码当正文

    collapsed: list[str] = []
    previous_blank = False
    for is_code, line in out:
        if is_code:
            collapsed.append(line.rstrip())
            previous_blank = False
            continue
        if not line.strip():
            if previous_blank:
                continue  # 正文里连续空行只留一个
            previous_blank = True
            collapsed.append("")
            continue
        previous_blank = False
        collapsed.append(line.rstrip())
    while collapsed and not collapsed[-1].strip():
        collapsed.pop()
    return "\n".join(collapsed)


def credentials_from_env() -> tuple[str, str]:
    """从环境变量取 `(app_id, app_secret)`（`.env` 由 `config.load_dotenv` 载入 `os.environ`）。"""
    return (
        os.environ.get(APP_ID_ENV, "").strip(),
        os.environ.get(APP_SECRET_ENV, "").strip(),
    )


def send_text(
    http: HttpClient,
    *,
    app_id: str,
    app_secret: str,
    chat_id: str,
    text: str,
    domain: str = DEFAULT_DOMAIN,
    receive_id_type: str = RECEIVE_ID_TYPE_CHAT,
) -> SendOutcome:
    """发一条文本消息到 `chat_id`。**失败回 `SendOutcome(ok=False)`，不抛异常。**

    流程：`fetch_tenant_token`（复用，已实测）→ `POST /open-apis/im/v1/messages?receive_id_type=…`。
    任何一步失败（取 token 失败 / 传输错误 / 回包非 JSON / `code != 0`）都返回带 `error` 的结果。

    `receive_id_type="open_id"` 时 `chat_id` 参数装的是**用户 open_id**（`ou_...`），
    这是私聊专用通道（`build_private_sender`）；默认仍是 `chat_id`，老调用点语义不变。
    """
    if not (app_id and app_secret):
        return SendOutcome(ok=False, error="缺凭据：FEISHU_APP_ID / FEISHU_APP_SECRET 未配置")
    if not (chat_id or "").strip():
        return SendOutcome(ok=False, error="缺 chat_id：没有可发送的目标")
    # **所有出站文本在这里统一纯文字化**（飞书 text 消息不解析 markdown）。
    # 放在 send_text 里而不是各调用点：回答 / 回执 / 配对提示 / 文件回执都走这一条路，
    # 漏一处就是一处格式乱码。
    plain = to_feishu_text(text)
    if not plain.strip():
        return SendOutcome(ok=False, error="剥掉 markdown 后正文为空，未发送")

    token = fetch_tenant_token(http, app_id=app_id, app_secret=app_secret, domain=domain)
    if token is None:
        return SendOutcome(ok=False, error="取 tenant_access_token 失败（凭据不对或网络错误）")

    body: dict[str, Any] = {
        "receive_id": chat_id,
        "msg_type": MSG_TYPE_TEXT,
        "content": text_content(plain),
        # 幂等键（SDK 同字段）：失败重发同一条不会重复投递
        "uuid": uuid.uuid4().hex,
    }
    try:
        result = http.post_json(
            send_message_url(domain, receive_id_type=receive_id_type),
            body,
            accept="application/json",
            cache=False,
            headers={"Authorization": f"Bearer {token.token}"},
        )
    except FetchError as exc:
        # **HTTP 4xx/5xx 不是"没有原因"**：飞书把 `code`/`msg` 全放在回包正文里
        # （实测 `HTTP 400 {"code":99992361,"msg":"open_id cross app"}`），
        # 而 `fetch/http.py` 抛错时正文本来会被丢掉 —— 于是日志里只剩"请求失败（400）"。
        # 这里把回包里的 code/msg 带进 `SendOutcome.code` 与 error（`explain_code` 附人话），
        # 让「99992361 open_id 跨应用」这种错一眼可见。**成功路径一个字节都没动。**
        code, msg = exc.platform_code()
        if code:
            return SendOutcome(
                ok=False,
                code=code,
                error=explain_code(code, msg) + f"（HTTP {exc.status or exc.kind}）",
            )
        return SendOutcome(ok=False, error=f"发送请求失败（{exc.kind}）：{exc}")

    try:
        payload = json.loads(result.text)
    except (json.JSONDecodeError, ValueError):
        return SendOutcome(ok=False, error=f"发送接口返回的不是 JSON：{result.text[:200]!r}")
    if not isinstance(payload, Mapping):
        return SendOutcome(ok=False, error="发送接口返回的不是 JSON 对象")

    code = int(payload.get("code") or 0)
    if code != 0:
        return SendOutcome(ok=False, code=code, error=f"飞书返回 code={code}：{payload.get('msg')}")
    data = payload.get("data")
    message_id = ""
    if isinstance(data, Mapping):
        message_id = str(data.get("message_id") or "")
    return SendOutcome(ok=True, code=0, message_id=message_id)


def build_private_sender(
    http: HttpClient | None,
    *,
    open_id: str,
    app_id: str = "",
    app_secret: str = "",
    domain: str = DEFAULT_DOMAIN,
) -> Callable[[str], dict[str, Any]] | None:
    """造一个**只发私聊**的发送器：``sender(text) -> dict``，目标钉死为这个 ``open_id``。

    为什么签名里没有收件人：跑完提醒（docs/20 §6.3）的硬约束是**只私聊、不发群**。
    如果把目标留成参数，调用方随时可能传一个群 ``chat_id`` 进来；把目标闭包在这里，
    "发群"在类型上就不可能发生 —— 而且用的是 ``receive_id_type=open_id``（用户级），
    不是 ``chat_id``。

    返回 ``None`` 表示**没有可发的通道**（缺凭据 / 没出网客户端 / 没有 open_id）：
    调用方据此一条都不发，而不是发了才失败。
    """
    resolved_id = (app_id or "").strip() or os.environ.get(APP_ID_ENV, "").strip()
    resolved_secret = (app_secret or "").strip() or os.environ.get(APP_SECRET_ENV, "").strip()
    target = (open_id or "").strip()
    if not (resolved_id and resolved_secret and http is not None and target):
        return None

    def _send(text: str) -> dict[str, Any]:
        try:
            outcome = send_text(
                http,
                app_id=resolved_id,
                app_secret=resolved_secret,
                chat_id=target,
                text=text,
                domain=domain,
                receive_id_type=RECEIVE_ID_TYPE_OPEN_ID,
            )
        except Exception as exc:  # 兜底：提醒发送失败绝不把轮次搞崩
            logger.warning("[Feishu] 私聊提醒发送异常（已吞）：open_id=%s err=%s", target, exc)
            return {
                "ok": False,
                "message_id": "",
                "code": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if outcome.ok:
            logger.info(
                "[Feishu] 私聊提醒已发送 open_id=%s message_id=%s", target, outcome.message_id
            )
        else:
            logger.warning(
                "[Feishu] 私聊提醒发送失败 open_id=%s code=%s：%s",
                target,
                outcome.code,
                outcome.error,
            )
        return outcome.as_dict()

    return _send


def _no_network_file_sender(reason: str) -> Callable[[str, str], dict[str, Any]]:
    """「不出网」的文件发送器（默认关 / 无凭据时用它）—— 只回错误，绝不上传。"""

    def _sender(open_id: str, path: str) -> dict[str, Any]:
        logger.warning(
            "[Feishu] 发文件未启用（%s）：未上传 open_id=%s path=%s", reason, open_id, path
        )
        return FileOutcome(ok=False, error=f"未配置文件发送器：{reason}").as_dict()

    return _sender


def build_file_sender(
    http: HttpClient | None,
    *,
    enabled: bool = False,
    app_id: str = "",
    app_secret: str = "",
    domain: str = DEFAULT_DOMAIN,
) -> Callable[[str, str], dict[str, Any]]:
    """造一个**渠道层形状**的文件发送器 `sender(open_id, path) -> dict`。

    与 :func:`build_sender` 同一套闸门：`enabled=False`（默认）/ 缺凭据 / 没出网客户端
    都**一次外网都不发**；任何异常都被吞成 `ok=False`（发文件失败不能把会话搞崩）。
    """
    resolved_id = (app_id or "").strip() or os.environ.get(APP_ID_ENV, "").strip()
    resolved_secret = (app_secret or "").strip() or os.environ.get(APP_SECRET_ENV, "").strip()

    if not enabled:
        return _no_network_file_sender("未开启出站发送（runtime.feishu_reply_enabled=false）")
    if not (resolved_id and resolved_secret and http is not None):
        reason = (
            "出网客户端未装配" if http is None else "缺凭据（FEISHU_APP_ID / FEISHU_APP_SECRET）"
        )
        return _no_network_file_sender(reason)

    def _send(open_id: str, path: str) -> dict[str, Any]:
        try:
            outcome = send_file(
                http,
                app_id=resolved_id,
                app_secret=resolved_secret,
                open_id=open_id,
                path=path,
                domain=domain,
            )
        except Exception as exc:  # 兜底：发文件失败绝不把会话/回调搞崩
            logger.warning("[Feishu] 发文件异常（已吞）：open_id=%s err=%s", open_id, exc)
            return FileOutcome(
                ok=False, file_name=Path(path).name, error=f"{type(exc).__name__}: {exc}"
            ).as_dict()
        if outcome.ok:
            logger.info(
                "[Feishu] 已发送文件 open_id=%s name=%s bytes=%d message_id=%s",
                open_id,
                outcome.file_name,
                outcome.size_bytes,
                outcome.message_id,
            )
        else:
            logger.warning("[Feishu] 发文件失败 open_id=%s：%s", open_id, outcome.error)
        return outcome.as_dict()

    return _send


def _no_network_sender(reason: str) -> Callable[[str, str], dict[str, Any]]:
    """「不出网」发送器：只告警、只回错误 —— **默认关 / 无凭据时用它**，绝不发任何外网。"""

    def _sender(chat_id: str, text: str) -> dict[str, Any]:
        logger.warning("[Feishu] 出站发送未启用（%s）：回复未发往飞书 chat_id=%s", reason, chat_id)
        return {"ok": False, "message_id": "", "code": 0, "error": f"未配置发送器：{reason}"}

    return _sender


def build_sender(
    http: HttpClient | None,
    *,
    enabled: bool = False,
    app_id: str = "",
    app_secret: str = "",
    domain: str = DEFAULT_DOMAIN,
) -> Callable[[str, str], dict[str, Any]]:
    """造一个**渠道层形状**的发送器 `sender(chat_id, text) -> dict`。

    - `enabled=False`（**默认**，对应 `runtime.feishu_reply_enabled=false`）→ 返回的发送器
      **一次外网都不发**，只告警并回 `{"ok": false, "error": ...}`（「默认关下不得发任何外网」）；
    - 凭据缺任一个、或没装配出网客户端 → 同样**不发外网**（只是原因不同）；
    - 否则每次调用走飞书 REST 发文本；**任何异常都被吞掉**并记日志（不崩会话）。
    """
    resolved_id = (app_id or "").strip() or os.environ.get(APP_ID_ENV, "").strip()
    resolved_secret = (app_secret or "").strip() or os.environ.get(APP_SECRET_ENV, "").strip()

    if not enabled:
        return _no_network_sender("未开启出站发送（runtime.feishu_reply_enabled=false）")
    if not (resolved_id and resolved_secret and http is not None):
        reason = (
            "出网客户端未装配" if http is None else "缺凭据（FEISHU_APP_ID / FEISHU_APP_SECRET）"
        )
        return _no_network_sender(reason)

    def _send(chat_id: str, text: str) -> dict[str, Any]:
        try:
            outcome = send_text(
                http,
                app_id=resolved_id,
                app_secret=resolved_secret,
                chat_id=chat_id,
                text=text,
                domain=domain,
            )
        except Exception as exc:  # 兜底：发送失败绝不把会话/回调搞崩
            logger.warning("[Feishu] 出站发送异常（已吞）：chat_id=%s err=%s", chat_id, exc)
            return {
                "ok": False,
                "message_id": "",
                "code": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if outcome.ok:
            logger.info("[Feishu] 已发送回复 chat_id=%s message_id=%s", chat_id, outcome.message_id)
        else:
            logger.warning(
                "[Feishu] 发送失败 chat_id=%s code=%s：%s", chat_id, outcome.code, outcome.error
            )
        return outcome.as_dict()

    return _send

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

from scholarscout.fetch.feishu_onboard import ONBOARD_OPEN_URLS, fetch_tenant_token
from scholarscout.fetch.http import FetchError, HttpClient

logger = logging.getLogger(__name__)

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
#: `receive_id_type`（本模块按 chat_id 发文本；**文件只能按 open_id 私聊发**）
RECEIVE_ID_TYPE_CHAT = "chat_id"
RECEIVE_ID_TYPE_OPEN_ID = "open_id"
#: 文本消息类型 / 文件消息类型
MSG_TYPE_TEXT = "text"
MSG_TYPE_FILE = "file"
#: 凭据环境变量名（键名照 Hermes `plugin.yaml:13-23` 与 `channels/feishu.py::credentials_from_env`）
APP_ID_ENV = "FEISHU_APP_ID"
APP_SECRET_ENV = "FEISHU_APP_SECRET"
#: 默认平台域名（feishu / lark，两套基址在 `fetch/feishu_onboard.py::ONBOARD_OPEN_URLS`）
DEFAULT_DOMAIN = "feishu"


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


def send_message_endpoint(domain: str = DEFAULT_DOMAIN) -> str:
    """发消息端点（开放平台基址 + `SEND_MESSAGE_PATH`）。未知域名回退 feishu（不抛）。"""
    base = ONBOARD_OPEN_URLS.get(domain, ONBOARD_OPEN_URLS["feishu"])
    return f"{base}{SEND_MESSAGE_PATH}"


def send_message_url(
    domain: str = DEFAULT_DOMAIN, *, receive_id_type: str = RECEIVE_ID_TYPE_CHAT
) -> str:
    """带 query 的完整发送 URL（`post_json` 只认 URL 本身带的 query）。"""
    return f"{send_message_endpoint(domain)}?receive_id_type={receive_id_type}"


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

"""抓取层契约类型：结果值对象 ``FetchResult`` 与错误类型 ``FetchError``。

为什么单独成模块（而不是留在 ``http.py``）：全项目依赖的**契约面**是这两个对象 ——
``providers/*``、``tools/source_fetch.py``、``rpc/archive.py``、``runtime/subtasks.py``
以及 ``fetch/`` 内的兄弟模块都只从它们取 ``kind`` / ``.text`` / ``.json()``，并不需要
``HttpClient`` 的请求、限速、缓存逻辑。钉在一个**零依赖**模块里（只用标准库 ``json`` /
``dataclasses`` / ``typing``）之后，契约的变更不再牵动出网关键路径。

兼容：``fetch.http`` 仍重导出 ``FetchError`` / ``FetchResult``，旧 import 路径
（``from scholarscout.fetch.http import FetchError``）不变。

错误归类口径（``FetchError.KINDS``）由 ``rpc/errors.py`` 映射成契约错误码；加/改
``kind`` 等于改协议，两边一起改。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = ["FetchError", "FetchResult"]


def _code_msg_from_text(text: str) -> tuple[int, str]:
    """从平台回包正文里取 `(code, msg)`；不是 JSON / 没有 `code` 一律回 `(0, "")`。

    只认**顶层** `code`（飞书的形状：`{"code":99992361,"msg":"open_id cross app",...}`），
    不猜嵌套、不猜别的键名 —— 猜错就会在日志里编出一个不存在的错误码。
    """
    raw = (text or "").strip()
    if not raw:
        return 0, ""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0, ""
    if not isinstance(payload, dict):
        return 0, ""
    try:
        code = int(payload.get("code") or 0)
    except (TypeError, ValueError):
        return 0, ""
    msg = payload.get("msg")
    return (code, str(msg)) if code else (0, "")


class FetchError(Exception):
    """抓取失败。``kind`` 让上层映射成契约错误码（见 rpc/errors.py）。"""

    KINDS = (
        "forbidden",  # 403 / 451，或 robots 不允许
        "rate_limited",  # 429
        "not_found",  # 404
        "server",  # 5xx
        "timeout",
        "network",
        "too_large",
        "bad_request",  # 其它 4xx
    )

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        url: str = "",
        status: int | None = None,
        body: str = "",
    ) -> None:
        if kind not in self.KINDS:
            raise ValueError(f"未知抓取错误类型：{kind!r}")
        super().__init__(message)
        self.kind = kind
        self.url = url
        self.status = status
        #: 平台回包正文的**截断片段**（默认空）。4xx/5xx 的语义常只写在回包里
        #: （飞书把 `code`/`msg` 全放 body，如 `99992361 open_id cross app`），
        #: 只留状态码会让上层「看得见失败、看不见原因」。这里只留证、不改消息文本。
        self.body = body

    def platform_code(self) -> tuple[int, str]:
        """从回包正文里取 `(code, msg)`（平台错误码口径；取不到回 `(0, "")`）。

        为什么放在错误对象上：`fetch/` 是唯一出网层，回包到这一层就被丢掉了；
        出站发送器（`fetch/feishu_send.py`）要报「飞书返回 99992361」就只能从这里拿。
        """
        return _code_msg_from_text(self.body)


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    content: bytes
    headers: dict[str, str] = field(default_factory=dict)
    media_type: str = ""
    from_cache: bool = False
    elapsed_ms: int = 0

    @property
    def charset(self) -> str:
        raw = self.media_type
        if "charset=" in raw:
            return raw.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        return "utf-8"

    @property
    def text(self) -> str:
        try:
            return self.content.decode(self.charset)
        except (LookupError, UnicodeDecodeError):
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

"""免 key JSON 检索源的共用件（crossref / datacite / doaj / europe_pmc / hn_algolia）。

为什么要有这一层：五个源的**成功形状各不相同**（``message.items`` / ``data`` / ``results`` /
``resultList.result`` / ``hits``），但**失败的表达必须一致** —— 上层只认 ``provider_status``
里的"这一条来源 ok 还是不 ok、为什么"（``docs/01 §5.1``）。

于是三种"拿到了回包却不能用"的情况统一在这里收敛成 :class:`FetchError`（可读理由），
而不是让裸异常穿出去：

1. 回包不是 JSON（网关拦截 / WAF 返回了 HTML）→ ``返回的不是合法 JSON``
2. 顶层不是对象（对方改成了数组）→ ``顶层不是对象``
3. 容器键消失或不是数组（对方改了契约）→ ``返回结构变了（缺少 message/items）``

**为什么不直接回空列表**：空列表的含义是"这次真的没搜到"，模型会据此认为"这个方向没资料"
并反复换词重搜（真机跑 E3 实测过）。结构变化必须说成"来源坏了"，两者不能混。
"""

from __future__ import annotations

from typing import Any

from scholarscout.fetch.contract import FetchError, FetchResult


def as_dict(value: Any) -> dict[str, Any]:
    """``dict`` 就返回，否则给空 dict —— 便于 mypy 收窄（同 ``openalex.py`` 的写法）。"""
    return value if isinstance(value, dict) else {}


def payload_of(response: FetchResult, *, label: str) -> dict[str, Any]:
    """取回包的 JSON 对象；不是 JSON 或顶层不是对象 → 可读的 ``FetchError``。

    用 ``bad_request`` 这个既有 kind（``providers/arxiv.py`` 对 XML 解析失败也是它）：
    ``FetchError.KINDS`` 是 ``rpc/errors.py`` 的协议面，**不新增 kind**（加 kind = 改协议）。
    """
    try:
        payload = response.json()
    except ValueError as exc:  # json.JSONDecodeError 是 ValueError 的子类
        snippet = response.text[:120].replace("\n", " ")
        raise FetchError(
            "bad_request",
            f"{label} 返回的不是合法 JSON（HTTP {response.status}）：{snippet or '空正文'}",
            url=response.url,
            status=response.status,
        ) from exc
    if not isinstance(payload, dict):
        raise FetchError(
            "bad_request",
            f"{label} 的返回顶层不是对象（拿到 {type(payload).__name__}）",
            url=response.url,
            status=response.status,
        )
    return payload


def list_at(payload: dict[str, Any], *path: str, label: str) -> list[Any]:
    """按路径取结果数组；路径缺键 / 不是数组 → 可读的 ``FetchError``。

    ``None`` 一律当空列表：有些源在 0 命中时给 ``null`` 而不是 ``[]``（不是契约变化）。
    """
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise FetchError(
                "bad_request",
                f"{label} 的返回结构变了（缺少 {'/'.join(path)}）",
            )
        node = node[key]
    if node is None:
        return []
    if not isinstance(node, list):
        raise FetchError(
            "bad_request",
            f"{label} 的 {'/'.join(path)} 不是数组（拿到 {type(node).__name__}）",
        )
    return node

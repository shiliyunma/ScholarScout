"""会话用量的两条读查询（`session.usage` 的取数口）。

原本写在 `rpc/session.py::session_usage`（白名单 `rpc/session.py: 2`），这一轮只是**位置**搬进
`state/`：列清单、聚合函数、`WHERE`、`GROUP BY`、`ORDER BY` 一个词没动。**业务边界仍留在 rpc 层**
—— 缓存命中率只在服务端确实报过缓存字段时才算（否则回 `None`，不是 0%）、单价没配就不报金额、
`by_purpose` 怎么摆给界面，全在 `session_usage` 里；这里只回**原始行**。

口径细在四处、写在 SQL 里都看不出来：

- ``count(cached_tokens)`` 而不是 ``count(*)``：只有**服务端返回过缓存字段**的行才计入
  ``calls_with_cache_field``。用 ``count(*)`` 会让"服务端不报缓存"被算成"命中率 0%"，
  看着像前缀缓存策略完全失效。
- ``coalesce(sum(est_cost),0.0)``：无单价的行 `est_cost` 为 NULL，`sum` 会整行变 NULL；
  这里兜成 0.0，但"没有价格"与"花了 0 元"要由调用方靠 ``calls`` 与金额分开判断，
  **不能**在这一层把 0.0 直接当金额报出去。
- ``GROUP BY 1`` 是**位置**引用：它绑的是第一列 ``coalesce(purpose,'?')``。往前面插一列
  （比如按 ``model`` 分组）就会静默改变分组键，所以列序也属于契约。
- ``ORDER BY calls DESC``：界面按这个顺序摆，调用次数最多的用途排最前；同调用次数之间的次序
  SQLite 不作保证，**刻意不补 ``rowid`` 兜底**（口径是"花了多少"，不是"先后写"）。
"""

from __future__ import annotations

from typing import Any

from scholarscout.state.db import Database

#: 会话累计：调用次数、三类 token、缓存字段的非空行数、金额。
_TOTALS_QUERY = """
    SELECT count(*) AS calls, coalesce(sum(prompt_tokens),0) AS prompt_tokens,
           coalesce(sum(completion_tokens),0) AS completion_tokens,
           coalesce(sum(cached_tokens),0) AS cached_tokens,
           count(cached_tokens) AS calls_with_cache_field,
           coalesce(sum(est_cost),0.0) AS est_cost FROM usage_records WHERE session_id = ?
"""

#: 按用途拆分：调用次数降序；``GROUP BY 1`` 绑的是 ``purpose`` 那一列（别插列）。
_BY_PURPOSE_QUERY = """
    SELECT coalesce(purpose,'?') AS purpose, count(*) AS calls,
           coalesce(sum(prompt_tokens),0) AS prompt_tokens,
           coalesce(sum(completion_tokens),0) AS completion_tokens
    FROM usage_records WHERE session_id = ? GROUP BY 1 ORDER BY calls DESC
"""


def session_usage_totals(db: Database, session_id: str) -> dict[str, Any]:
    """这个会话在 ``usage_records`` 里的累计行（聚合，永远有一行；一条记录都没有时全为 0）；空结果回空字典。"""
    row = db.query_one(_TOTALS_QUERY, (session_id,))
    return dict(row) if row is not None else {}


def session_usage_by_purpose(db: Database, session_id: str) -> list[dict[str, Any]]:
    """这个会话按 ``purpose`` 拆分的用量行（``purpose`` 为 NULL 的一律记作 ``'?'``），调用次数降序；无记录回空列表。"""
    return [dict(row) for row in db.query(_BY_PURPOSE_QUERY, (session_id,))]

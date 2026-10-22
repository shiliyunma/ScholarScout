"""`py.exec` 的审批面：`sandbox.rules.list` / `sandbox.rules.revoke` / `sandbox.approve`（docs/24 §5）。

**为什么要这三个方法**：沙箱执行器与审批门（`sandbox/{runner,approval,rules}.py`）早就实现了
「类别 × 范围」的规则记忆与四选项授权，但**没有任何 RPC 把它们暴露给界面** ——
后果有两层，都是实测的：

1. 设置界面看不到「我到底永久放行了什么」，更撤不回来（规则只在文件里）；
2. 工具回 `ok=false + data.kind="needs_approval"` 之后，**没有东西能把用户的选择送回去**，
   于是「一次都不会真执行」——卡片能画出来，点下去却没有入口。

三个方法的分工（与 docs/01 §3.12 一一对应）：

- ``sandbox.rules.list``：读规则（**每次回磁盘**，设置界面看到的就是文件里的真值）。
  永久规则来自 ``run/sandbox_rules.json``，本次会话的授权来自审批门的内存字典 —— 两条来源
  分开给，界面不必自己合并。
- ``sandbox.rules.revoke``：删一条（永久与本会话都撤），回 ``{removed}``；没这条就 ``false``，
  界面据此回"这条已经不在"。
- ``sandbox.approve``：**用户的选择回填点**。带着 ``approval`` 重新调同一个工具处理器
  （`tools/py_exec.py::py_exec`），把「这次执不执行」变成真的——``once``/``session``/``always``
  会真的起子进程跑那段代码，``deny`` 走审批门的拒绝分支（计入连拒、带"别绕"那句）。

**为什么不复用 `chat.send` 让模型自己带 `approval`**：提示词明写「不要自己填 `approval`」——
让模型转述用户授权，等于把许可证交回给模型。这个入口把参数直接交给审批门，模型的输入不参与。

**参数校验从严**（`-32602`）：``approval`` 只认四个值、``code`` 必须非空字符串。
宽松会把"拼错的 approval"当成缺省值（=再问一次），用户点了没反应却查不出原因。
"""

from __future__ import annotations

from typing import Any

from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.registry import AppContext, _params, method
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.sandbox.approval import (
    APPROVAL_LEVELS,
    CONTENT_RISK_CATEGORIES,
    ApprovalGate,
)
from scholarscout.tools import py_exec as py_exec_tool


def _gate() -> ApprovalGate:
    """本进程的审批门（`session` 授权与连拒计数必须跨调用累积，不能每次新建）。"""
    return py_exec_tool.approval_gate()


def _session(params: dict[str, Any]) -> str | None:
    value = str(params.get("session_id") or "").strip()
    return value or None


def _scope(key: str) -> str:
    """规则键 ``write:exports/**`` → 范围 ``exports/**``（界面按标签显示）。"""
    _, _, scope = key.partition(":")
    return scope


def _rule_row(key: str, level: str, created_at: str = "") -> dict[str, Any]:
    """界面要的一行。``downgraded`` = 这条**只到本会话**且原因是内容级风险不可永久放行。"""
    category, _, _ = key.partition(":")
    return {
        "key": key,
        "scope": _scope(key),
        "level": level,
        "created_at": created_at,
        "downgraded": level == "session" and category in CONTENT_RISK_CATEGORIES,
    }


@method("sandbox.rules.list")
def sandbox_rules_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """列出审批规则：永久（落盘）+ 本会话（内存），两条来源分开给。"""
    params = _params(params)
    listed = _gate().list_rules(session_id=_session(params))
    # 永久在前：用户来这一栏通常是查"我给过什么永久许可"；同类按 key 排序，位置稳定
    always = sorted(listed["always"], key=lambda item: str(item.get("key", "")))
    session = sorted(listed["session"], key=lambda item: str(item.get("key", "")))
    rules = [
        _rule_row(str(item["key"]), "always", str(item.get("created_at", ""))) for item in always
    ]
    rules += [_rule_row(str(item["key"]), "session") for item in session]
    return {"rules": rules}


@method("sandbox.rules.revoke")
def sandbox_rules_revoke(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """撤销一条规则（永久与本会话都撤）；``removed=false`` 表示本来就没有这条。"""
    params = _params(params)
    key = params.get("key")
    if not isinstance(key, str) or not key.strip():
        raise RpcError(Code.INVALID_PARAMS, "key 必须是非空字符串（形如 write:exports/**）")
    removed = _gate().revoke(key.strip(), session_id=_session(params))
    return {"removed": removed}


@method("sandbox.approve")
def sandbox_approve(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """用户点完审批卡之后的那一次重调：带 ``approval`` 跑同一个工具处理器。

    回包形状 = ``{ok, approval, error, data}``：``data`` 就是工具信封的 ``data``
    （``once``/``session``/``always`` 时含 ``stdout`` / ``stderr`` / ``returncode``，
    ``deny`` 时含 ``kind="rejected"`` 与 ``denial_streak``）—— 界面不必再解析一遍。
    """
    params = _params(params)
    approval = params.get("approval")
    if approval not in APPROVAL_LEVELS:
        raise RpcError(
            Code.INVALID_PARAMS,
            f"approval 只认 {'/'.join(APPROVAL_LEVELS)}，收到 {approval!r}",
        )
    code = params.get("code")
    if not isinstance(code, str) or not code.strip():
        raise RpcError(Code.INVALID_PARAMS, "code 必须是非空字符串（把待执行的原样代码带回来）")
    workdir = params.get("workdir")
    if workdir is not None and not isinstance(workdir, str):
        raise RpcError(Code.INVALID_PARAMS, "workdir 必须是字符串或省略")
    raw_timeout = params.get("timeout_s")
    timeout_s: float | None = None
    if raw_timeout is not None:
        if not isinstance(raw_timeout, int | float) or isinstance(raw_timeout, bool):
            raise RpcError(Code.INVALID_PARAMS, "timeout_s 必须是数字或省略")
        timeout_s = float(raw_timeout)

    tool_ctx = ToolContext(
        db=ctx.db,
        config=ctx.config,
        http=ctx.http,
        llm=ctx.llm,
        session_id=_session(params),
        # 没有轮次：这是界面发起的一次重调，不属于模型那一轮（事件仍带 session_id）
        turn_id="",
    )
    result = py_exec_tool.py_exec(
        tool_ctx,
        code=code,
        workdir=workdir,
        timeout_s=timeout_s,
        approval=approval,
    )
    data = result.get("data")
    return {
        "ok": bool(result.get("ok")),
        "approval": approval,
        "error": str(result.get("error") or ""),
        "data": data if isinstance(data, dict) else {},
    }

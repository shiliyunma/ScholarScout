"""``sandbox.rules.list`` / ``sandbox.rules.revoke`` / ``sandbox.approve`` 的契约（docs/01 §3.12）。

为什么这四个用例值得单列：审批规则**只在内存与一个 JSON 文件里**，界面上"我到底永久放行了什么"
与"撤销"全靠这三条 RPC。少了其中任何一条，用户就只能去手改 ``run/sandbox_rules.json`` ——
而那个文件恰恰是 ``py.exec`` 的硬底线（不许代码碰它），让人手改等于承认界面做不了这件事。

四条断言各钉一件事（对应 docs/01 §3.12 那张表）：

1. 没有任何规则时 ``rules.list`` 回**空列表**（不是抛错、不是 ``null``）；
2. ``revoke`` 删存在的 key → ``removed:true``，删不存在的 → ``removed:false`` 且**不报错**
   （撤销是幂等的；抛错会让界面把"本来就没有"显示成故障）；
3. ``sandbox.approve`` 的 ``approval`` 非法 → ``-32602``（宽松会把拼错当成"再问一次"，
   用户点了没反应却查不出原因）；
4. 三个方法都真的注册进了方法表（漏注册 = 文档有、代码没有，客户端拿到 ``-32601``）。

规则文件落在 ``SCHOLARSCOUT_HOME`` 重定向后的 ``run/`` 下，用例之间互不串味。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.methods import AppContext, dispatch, method_names
from scholarscout.tools import py_exec as py_exec_tool

from ..conftest import redirect_home

#: 本节的三个方法（docs/01 §3.12）
SANDBOX_METHODS = ("sandbox.rules.list", "sandbox.rules.revoke", "sandbox.approve")


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """规则文件写到 tmp 的 ``run/`` 下（绝不碰仓库里的真规则表）。"""
    return redirect_home(monkeypatch, tmp_path)


def test_rules_list_is_empty_when_nothing_was_granted(ctx: AppContext) -> None:
    """一条规则都没有 → 空列表（界面据此显示"还没给过任何永久许可"）。"""
    assert dispatch(ctx, "sandbox.rules.list", {}) == {"rules": []}


def test_revoke_existing_rule_reports_removed_and_missing_one_is_not_an_error(
    ctx: AppContext,
) -> None:
    """删存在的 → ``removed:true`` 且真的不在表里了；删不存在的 → ``removed:false`` **不抛**。"""
    gate = py_exec_tool.approval_gate()
    gate.store.grant("write:exports/**", level="always", origin="test")

    listed = dispatch(ctx, "sandbox.rules.list", {})
    assert [row["key"] for row in listed["rules"]] == ["write:exports/**"]
    # 行的形状逐字段对齐契约：类别前缀被拆成 scope，级别如实写 always
    assert listed["rules"][0]["scope"] == "exports/**"
    assert listed["rules"][0]["level"] == "always"
    assert listed["rules"][0]["downgraded"] is False

    assert dispatch(ctx, "sandbox.rules.revoke", {"key": "write:exports/**"}) == {"removed": True}
    assert dispatch(ctx, "sandbox.rules.list", {}) == {"rules": []}

    # 幂等：这条已经不在，如实回 false，而不是抛"找不到"
    assert dispatch(ctx, "sandbox.rules.revoke", {"key": "write:exports/**"}) == {"removed": False}


def test_approve_rejects_unknown_approval_value(ctx: AppContext) -> None:
    """``approval`` 只认四个值；拼错/缺省一律 ``-32602``（不能静默当成"再问一次"）。"""
    for bad in ("yolo", "", 1, None):
        with pytest.raises(RpcError) as exc:
            dispatch(ctx, "sandbox.approve", {"approval": bad, "code": "print(1)"})
        assert exc.value.code == Code.INVALID_PARAMS, f"approval={bad!r} 应该报 -32602"


def test_three_sandbox_methods_are_registered(ctx: AppContext) -> None:
    """三个方法都在 ``method_names()`` 里（漏注册 = 客户端拿到 -32601）。"""
    registered = set(method_names())
    missing = [name for name in SANDBOX_METHODS if name not in registered]
    assert missing == [], f"这些方法没注册：{missing}"

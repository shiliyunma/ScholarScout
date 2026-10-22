"""``py.exec`` 工具层契约：**审批门与执行器接起来之后对外长什么样**。

为什么这一层要单独测（``tests/sandbox/`` 已经把审批门与执行器各自测透了）：
夹在中间的这一层是**唯一**决定\"模型看到什么\"的地方 —— 同一次拒绝，
`sandbox.approval` 返回的是 :class:`Decision` 对象，而模型收到的是
``{ok: false, error: 人话, data: {...}, refs: {}}``。两条最容易出事的边界就在这里：

1. **不抛**：主循环把任何冒泡异常当\"工具坏了\"。而 `needs_approval`（等用户点一下）
   与超时/内存超限（代码自己跑坏）都是**正常结果**，必须以 ``ok=false`` + 非空 ``error``
   回到循环里，而不是变成 ``ToolError``；
2. **信封同形**：成功 / 待确认 / 被拒 / 失败四种回包的顶层键集一致（``ok/data/refs``
   或 ``ok/error/data/refs``），前端与 ``tests/contract/test_tool_envelope.py`` 都按它读。

全部用**本文件里写死的固定小片段**（打印一行、写一个小文件、死循环、要一大块内存），
一条危险操作都不跑。``workdir`` 走缺省的 ``data/exports``，落盘根被 ``redirect_home``
重定向到 ``tmp_path``，所以不会往仓库里写东西。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout import paths
from scholarscout.rpc.methods import AppContext
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.tools.py_exec import PARAMETERS, SPEC, approval_gate, py_exec

from ..conftest import redirect_home

#: 一个\"改文件 + 打印\"的普通片段（可审批、可执行、结果可断言）
EDIT_SNIPPET = 'open("out.txt", "w", encoding="utf-8").write("hi")\nprint("done")\n'
#: 硬底线样本：凭据文件。选它而不是系统目录，是因为它**不产生任何副作用**就能命中，
#: 且与 ``.env`` 那条规矩一一对应（docs/24 §3）。
ENV_SNIPPET = 'open(".env", "a", encoding="utf-8").write("LLM_API_KEY=x")\n'


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """落盘根重定向到 tmp —— 规则文件（``run/sandbox_rules.json``）也跟着进 tmp。

    审批门按 ``rules_path()`` 换实例，所以每个用例天然是\"干净的门\"（无 session 授权、连拒计数归零）。
    """
    return redirect_home(monkeypatch, tmp_path)


def _ctx(ctx: AppContext, session: str = "ses_py_exec") -> ToolContext:
    """工具层要的上下文（与真实调用同一形状，只额外指定会话 id）。"""
    return ToolContext(db=ctx.db, config=ctx.config, session_id=session)


def _exports(home: Path) -> Path:
    """缺省工作目录（``data/exports``）的绝对路径。"""
    return home / "data" / "exports"


# --------------------------------------------------------------------------- #
# 1. 无授权 → needs_approval，且**不抛**
# --------------------------------------------------------------------------- #


def test_without_approval_returns_needs_approval_and_does_not_raise(
    ctx: AppContext, tmp_path: Path
) -> None:
    """第一次调用（不带 ``approval``）回 ``ok=false`` + ``kind=needs_approval``，代码一行都不执行。

    \"不抛\"是这条用例的全部意义：等用户点确认是正常流程，冒泡成异常就会被主循环记成工具故障，
    用户还没看到确认框就先看到\"工具出错\"。
    """
    result = py_exec(_ctx(ctx), code=EDIT_SNIPPET)  # 不包 pytest.raises：抛了就算失败

    assert result["ok"] is False
    assert set(result) == {"ok", "error", "data", "refs"}
    assert result["refs"] == {}
    assert result["error"].strip()  # 非空人话（模型据此知道\"要等确认\"）

    data = result["data"]
    assert data["kind"] == "needs_approval"
    assert data["pending"] == ["exec:@cwd", "write:@cwd"]
    assert [risk["key"] for risk in data["risks"]] == ["exec:@cwd", "write:@cwd"]
    assert [option["value"] for option in data["options"]] == ["deny", "once", "session", "always"]
    assert data["workdir"] == "data/exports"
    # 没执行：文件没被创建（needs_approval 是\"问一句\"，不是\"先跑了再说\"）
    assert not (_exports(tmp_path) / "out.txt").exists()


# --------------------------------------------------------------------------- #
# 2. 硬底线：任何授权都绕不过
# --------------------------------------------------------------------------- #


def test_hard_floor_rejects_even_when_every_category_is_pre_authorized(
    ctx: AppContext, tmp_path: Path
) -> None:
    """先把**所有**类别永久授权，再传 ``approval=always`` —— 仍然 ``rejected``。

    这是结构性的证明而不是口头承诺：``ApprovalGate.check`` 在读规则**之前**调 ``hard_floor``，
    命中就直接返回拒绝；``hard_floor`` 的返回值里也只有拒绝理由或 ``None``，没有\"允许\"这个值。
    """
    gate = approval_gate()
    for category in (
        "exec",
        "write",
        "delete",
        "read",
        "network",
        "subprocess",
        "native",
        "outside_path",
        "dynamic",
    ):
        gate.store.grant(f"{category}:@any", level="always", origin="test")
        gate.store.grant(f"{category}:@cwd", level="always", origin="test")

    result = py_exec(_ctx(ctx), code=ENV_SNIPPET, approval="always")

    assert result["ok"] is False
    assert set(result) == {"ok", "error", "data", "refs"}
    assert result["data"]["kind"] == "rejected"
    assert ".env" in result["error"]  # 理由说清命中了什么
    assert result["data"]["risks"] == []  # 硬底线不产出风险清单：它不问，只拒
    assert not (_exports(tmp_path) / ".env").exists()


# --------------------------------------------------------------------------- #
# 3. 授权后正常执行 → 成功信封
# --------------------------------------------------------------------------- #


def test_once_approval_runs_the_snippet_and_returns_success_envelope(
    ctx: AppContext, tmp_path: Path
) -> None:
    """``approval=once``：真的跑起来，回 ``{ok: true, data: {stdout, returncode, duration_ms, …}}``；
    且 ``once`` **不留痕** —— 同样的代码再调一次（不带 approval）仍然要问。
    """
    asked = py_exec(_ctx(ctx), code=EDIT_SNIPPET)
    assert asked["data"]["kind"] == "needs_approval"

    result = py_exec(_ctx(ctx), code=EDIT_SNIPPET, approval="once")

    assert result["ok"] is True
    assert set(result) == {"ok", "data", "refs"}
    assert result["refs"] == {}

    data = result["data"]
    assert data["stdout"].strip() == "done"
    assert data["stderr"] == ""
    assert data["returncode"] == 0
    assert isinstance(data["duration_ms"], int)
    assert data["duration_ms"] >= 0
    assert data["timed_out"] is False
    assert data["killed"] is False
    assert data["violations"] == []
    assert data["workdir"] == "data/exports"
    assert data["approval"]["kind"] == "allowed"
    assert data["approval"]["downgraded"] is False
    assert "只允许这一次" in data["approval"]["reason"]
    # 代码真跑了：文件落在 data/exports 下，内容对得上
    assert (_exports(tmp_path) / "out.txt").read_text(encoding="utf-8") == "hi"
    # once 不写规则文件：下一次同款调用回到\"要问\"
    assert py_exec(_ctx(ctx), code=EDIT_SNIPPET)["data"]["kind"] == "needs_approval"


# --------------------------------------------------------------------------- #
# 4. 超时 / 内存超限 → 可读失败，异常不冒泡
# --------------------------------------------------------------------------- #


def test_timeout_is_reported_as_readable_failure(ctx: AppContext) -> None:
    """``while True: pass`` + 1 秒超时：``ok=false``、``timed_out=True``、``error`` 说清\"超时被杀\"。"""
    result = py_exec(
        _ctx(ctx),
        code="while True:\n    pass\n",
        timeout_s=1,
        approval="once",
    )

    assert result["ok"] is False
    assert set(result) == {"ok", "error", "data", "refs"}
    assert result["data"]["kind"] == "failed"
    assert result["data"]["timed_out"] is True
    assert result["data"]["returncode"] != 0
    assert "超时被杀" in result["error"]
    assert result["error"].strip()


def test_memory_limit_failure_is_readable(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """内存上限压到 128MB 后要 600MB：子进程失败，工具层给**可读**失败而不是抛异常。

    为什么把上限改成 128（而不是用缺省 512）：512MB 要真分配半个 GB 才失败，128MB 当场就失败 ——
    验的是\"超限 → 可读失败\"这条链路，不是内核页面的分配速度。上限本身由
    ``tests/sandbox/test_runner.py::test_memory_limit_kills_the_child_but_not_the_parent`` 钉着。
    """
    monkeypatch.setattr("scholarscout.tools.py_exec.DEFAULT_MEM_MB", 128)

    result = py_exec(
        _ctx(ctx),
        code="block = bytearray(600 * 1024 * 1024)\nprint(len(block))\n",
        approval="once",
    )

    assert result["ok"] is False
    assert set(result) == {"ok", "error", "data", "refs"}
    assert result["data"]["kind"] == "failed"
    assert result["data"]["timed_out"] is False  # 不是超时，是内存没拿到
    assert result["data"]["returncode"] != 0
    assert "执行失败" in result["error"]
    assert result["error"].strip()


# --------------------------------------------------------------------------- #
# 5. 声明形状（工具声明是对外契约的一部分）
# --------------------------------------------------------------------------- #


def test_spec_shape() -> None:
    """工具名、必填项、四选项枚举、不许额外属性 —— 改这些就是改契约。"""
    assert SPEC.name == "py.exec"
    assert SPEC.toolset == "research"
    assert PARAMETERS["required"] == ["code"]
    assert set(PARAMETERS["properties"]) == {"code", "workdir", "timeout_s", "approval"}
    assert PARAMETERS["properties"]["approval"]["enum"] == ["deny", "once", "session", "always"]
    assert PARAMETERS["additionalProperties"] is False


def test_default_workdir_is_under_data(ctx: AppContext) -> None:
    """缺省工作目录落在 ``data/`` 下；``data/`` 外的路径直接拒（不审批、不执行）。"""
    outside = paths.home().parent
    result = py_exec(_ctx(ctx), code="print(1)\n", workdir=str(outside))

    assert result["ok"] is False
    assert result["data"]["kind"] == "invalid"
    assert "data/" in result["error"]

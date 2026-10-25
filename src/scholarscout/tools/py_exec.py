"""``py.exec`` —— 模型写 Python 去改 docx/xlsx/pdf 的执行入口（docs/24）。

**它是什么**：一个"写段 Python、在指定工作目录里跑一遍"的把手，主要用来处理
``python-docx`` / ``openpyxl`` / ``pymupdf`` 这类"只有代码才改得动"的产物。
**它不是什么**：不是安全边界（docs/24 §2）。真正的护栏是**运行前的人工确认** ——
工具本身只是把"能不能跑"的判断（硬底线 / 风险清单 / 规则记忆 / 熔断）与"怎么跑"（子进程 +
Job Object + audit hook）如实实现出来。

调用形状（两段式，**模型不能自己给自己发许可证**）：

1. 模型第一次调 ``py.exec``（不带 ``approval``）→ 回 ``ok=false`` +
   ``data.kind="needs_approval"``、``data.risks``、``data.options``；
2. 界面拿四个选项问用户，用户点完，上层带着 ``approval="deny"|"once"|"session"|"always"``
   **重新调用同一个工具**；这次才真的执行。

为什么 ``needs_approval`` 是``ok=false`` 而不是异常：主循环把任何冒泡的异常当工具故障，
而这里"等用户点一下"是**正常流程**。工具层一条异常都不抛（除参数类型非法，由 rpc 映射成 ``-32602``）。
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from scholarscout import paths
from scholarscout.library import artifacts as artifact_index
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.sandbox.approval import ApprovalGate, approval_options
from scholarscout.sandbox.rules import RuleStore, rules_path
from scholarscout.sandbox.runner import (
    DEFAULT_MAX_OUTPUT,
    DEFAULT_MEM_MB,
    DEFAULT_TIMEOUT_S,
    MAX_TIMEOUT_S,
    run_python,
)
from scholarscout.tools.envelope import ToolResult, fail, ok

DESCRIPTION = (
    "在工作目录里运行一段 Python，用来读写 docx/xlsx/pdf 这类只有代码才改得动的文件。"
    "**第一次调用必须不带 approval**：工具会回 ok=false + data.kind=needs_approval，"
    "把 data.risks（要动什么）交给用户确认；用户点完，上层会用 approval=deny/once/session/always "
    "重新调用同一个工具，那次才真的执行。"
    "workdir 只允许 data/ 下的子目录（缺省 data/exports；相对 data/ 给 exports 或 data/exports 都一样，"
    "不会重复拼接）；网络、子进程、原生调用一律被拦；"
    "只能读写工作目录内的文件。被拒时按 error 里的说明停下，不要换写法重试。"
)

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "要执行的 Python 源码（整段脚本，不是表达式）",
        },
        "workdir": {
            "type": "string",
            "description": (
                "工作目录，只允许 data/ 下的子目录；缺省 data/exports。"
                "相对 data/ 写 exports 即可（写 data/exports 也按 data/exports 处理，不会重复拼接）；"
                "也可给 data/ 下的绝对路径"
            ),
        },
        "timeout_s": {
            "type": "number",
            "description": f"超时秒数，1-{int(MAX_TIMEOUT_S)}，缺省 {int(DEFAULT_TIMEOUT_S)}",
        },
        "approval": {
            "type": "string",
            "enum": ["deny", "once", "session", "always"],
            "description": "用户的选择：deny=不许执行、once=只这一次、session=本会话同类操作、always=永远同类（内容级风险会降级为本会话）",
        },
    },
    "required": ["code"],
    "additionalProperties": False,
}

#: 审批门的进程内单例 —— ``session`` 授权与连拒计数必须跨调用累积，不能每次新建一个
_GATE: ApprovalGate | None = None
_GATE_PATH: Path | None = None


def approval_gate() -> ApprovalGate:
    """取本进程的审批门（落盘路径变了——比如测试重定向了 home——就换一个新的）。"""
    global _GATE, _GATE_PATH
    path = rules_path()
    if _GATE is None or path != _GATE_PATH:
        _GATE = ApprovalGate(RuleStore(path))
        _GATE_PATH = path
    return _GATE


def _data_root() -> Path:
    return paths.data_dir().resolve()


def _resolve_workdir(workdir: str | None) -> tuple[Path | None, str]:
    """把参数里的 workdir 收在 ``data/`` 下。返回 ``(路径, 错误文案)``，两者必有一个为空。

    **相对路径的基准是 ``data/``**，但入参里重复写 ``data/`` 不算错：模型读到的描述是
    "只允许 data/ 下的子目录（相对 data/）"，最自然的抄法就是 ``data/exports`` ——
    旧实现直接拿它去拼 ``root``，得到 ``<home>/data/data/exports``，**审批卡上显示的就是这个
    错路径**（用户看不出工具到底要在哪儿动手）。所以先剥掉与数据根同名的前导分量，再拼。
    """
    root = _data_root()
    if workdir is None or not str(workdir).strip():
        return root / "exports", ""
    try:
        candidate = Path(str(workdir).strip()).expanduser()
        if not candidate.is_absolute():
            candidate = _under_root(root, candidate)
        resolved = candidate.resolve()
    except (OSError, ValueError) as exc:
        return None, f"workdir 不合法：{exc}"
    if resolved != root and root not in resolved.parents:
        return None, f"workdir 只能是 data/ 下的子目录，收到的是 {resolved}"
    return resolved, ""


def _under_root(root: Path, relative: Path) -> Path:
    """相对路径按 ``data/`` 解析，并剥掉入参里已写过的 ``data`` 前缀（不区分大小写）。

    ``data/exports`` / ``Data/exports`` / ``data/data/exports`` → ``<data>/exports``；
    ``exports`` → ``<data>/exports``。数据根名字从 ``root`` 取，不在代码里写死 "data"。
    """
    parts = relative.parts
    while parts and parts[0].lower() == root.name.lower():
        parts = parts[1:]
    return root.joinpath(*parts) if parts else root


def _timeout(value: float | None) -> float:
    if value is None:
        return DEFAULT_TIMEOUT_S
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"timeout_s 必须是数字，收到 {value!r}") from exc
    return max(1.0, min(seconds, MAX_TIMEOUT_S))


def _approval_event(ctx: ToolContext, *, code: str, data: dict[str, Any], reason: str) -> None:
    """把「需要用户点头」推给界面（事件名 ``chat.needs_approval``，已进 ``NOTIFICATION_METHODS``）。

    为什么单开一个事件、而不是让前端从 ``chat.tool_result`` 里反解：
    卡片要**完整代码**、``pending`` 清单与四个选项，而工具结果事件只带 ``ok`` / ``digest`` ——
    让前端去猜等于把契约藏在摘要里。载荷与 `chat.needs_choice` 同构（``turn_id`` + 会话由
    ``bind_emitter`` 注入），前端按它渲染卡片、去重键是 ``turn_id|code``。

    事件失败**不能连累工具回包**（界面没接上时，模型仍要如实地收到 ``needs_approval``）。
    """
    emit = ctx.emit
    if emit is None:
        return
    with contextlib.suppress(Exception):
        emit(
            "chat.needs_approval",
            {
                "turn_id": ctx.turn_id,
                "session_id": ctx.session_id,
                "code": code,
                "reason": reason,
                "workdir": data.get("workdir", ""),
                "pending": data.get("pending", []),
                "risks": data.get("risks", []),
                "options": data.get("options", []),
            },
        )


def py_exec(
    ctx: ToolContext,
    code: str,
    workdir: str | None = None,
    timeout_s: float | None = None,
    approval: str | None = None,
) -> ToolResult:
    """审批 → 执行 → 如实回包。异常一律就地收敛成 ``ok=false``。"""
    text = (code or "").strip()
    if not text:
        return fail("py_exec_empty", "code 是空的：要执行什么？", data={"kind": "invalid"})

    target, error = _resolve_workdir(workdir)
    if target is None:
        return fail("py_exec_workdir", error, data={"kind": "invalid"})

    gate = approval_gate()
    decision = gate.check(text, workdir=target, session_id=ctx.session_id, approval=approval)
    risks = [risk.as_dict() for risk in decision.risks]

    if decision.kind == "needs_approval":
        data = {
            "kind": "needs_approval",
            "risks": risks,
            "pending": decision.pending,
            "options": approval_options(),
            "workdir": paths.rel_to_home(target),
        }
        _approval_event(ctx, code=text, data=data, reason=decision.reason)
        return fail("needs_approval", "这次执行需要用户确认：" + decision.reason, data=data)
    if decision.kind == "rejected":
        return fail(
            "py_exec_rejected",
            decision.reason,
            data={
                "kind": "rejected",
                "risks": risks,
                "denial_streak": decision.denial_streak,
                "workdir": paths.rel_to_home(target),
            },
        )

    result = run_python(
        text,
        workdir=target,
        timeout_s=_timeout(timeout_s),
        mem_mb=DEFAULT_MEM_MB,
        max_output=DEFAULT_MAX_OUTPUT,
    )
    payload: dict[str, Any] = {
        **result.as_dict(),
        "workdir": paths.rel_to_home(target),
        "risks": risks,
        "approval": {
            "kind": "allowed",
            "downgraded": decision.downgraded,
            "reason": decision.reason,
        },
    }
    # 产物落盘 → **立刻**增量入库并刷新资料索引（docs/23 缺陷 1：生成完看不见）。
    # 失败只让回包标 `indexed=false`，产物仍算成功 —— 索引是附加动作，不许拖垮主流程。
    payload.update(
        artifact_index.refresh_after_write(
            ctx.db,
            target,
            session_id=ctx.session_id,
            layout=ctx.config.library.layout,
            emit=ctx.emit,
        )
    )
    if result.violations:
        blocked = "、".join(item.get("event", "?") for item in result.violations[:5])
        return fail(
            "py_exec_blocked",
            f"执行结束，但有 {len(result.violations)} 处调用被沙箱拦下（{blocked}）。"
            "越界的读写/网络/子进程/原生调用一律不生效；按这个限制改代码，"
            "不要换写法重试，也不要换条路达成同一目的。",
            data={**payload, "kind": "blocked"},
        )
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or "没有输出"
        return fail(
            "py_exec_failed",
            f"执行失败（returncode={result.returncode}"
            + ("，超时被杀" if result.timed_out else "")
            + f"）：{detail[-800:]}",
            data={**payload, "kind": "failed"},
        )
    return ok(payload)


SPEC = ToolSpec(
    name="py.exec",
    description=DESCRIPTION,
    parameters=PARAMETERS,
    handler=py_exec,
    toolset="research",
)


def register() -> None:
    from scholarscout.runtime import registry

    registry.register(SPEC, replace=True)

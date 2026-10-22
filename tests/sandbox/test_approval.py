"""审批门五件事：硬底线不可绕过、四选项、规则记忆（类别×范围）、内容级降级、连拒熔断。

硬底线那一组是全篇的重点：它不是"再问一次"，而是**任何授权都到不了放行分支**。
所以每条硬底线样本都用"把所有风险键都预先永久授权 + 传 approval=always"来打，
断言仍然 ``rejected`` —— 这是结构性的（``hard_floor`` 的返回值里没有"允许"，
``ApprovalGate.check`` 在它返回非空时直接拒，根本不读规则）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout.sandbox.approval import (
    APPROVAL_LEVELS,
    MAX_CONSECUTIVE_DENIALS,
    REFUSAL_TAIL,
    ApprovalGate,
    Risk,
    approval_options,
    classify,
    hard_floor,
    rule_covers,
)
from scholarscout.sandbox.rules import RuleStore

NO_CONTENT_RISK = ("exec", "write", "delete", "read")


def _gate(tmp_path: Path) -> ApprovalGate:
    """每个用例一个干净的门（规则文件在 tmp 里，互不串味）。"""
    return ApprovalGate(RuleStore(tmp_path / "run" / "sandbox_rules.json"))


def _grant_everything(gate: ApprovalGate, code: str, workdir: Path) -> None:
    """把这条代码的**所有**风险键都预先永久授权（用来证明硬底线照样拒绝）。"""
    for risk in classify(code, workdir):
        gate.store.grant(risk.key(), level="always", origin="test")
    for category in (
        "exec",
        "write",
        "delete",
        "network",
        "subprocess",
        "native",
        "outside_path",
        "dynamic",
    ):
        gate.store.grant(f"{category}:@any", level="always", origin="test")


# --------------------------------------------------------------------------- #
# 1. 硬底线：命中即终局
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "snippet"),
    [
        ("系统目录", 'open(r"C:\\Windows\\win.ini", "w").write("x")'),
        ("环境变量形式的系统目录", 'open("%WINDIR%\\\\win.ini", "w")'),
        ("凭据文件", 'open(".env", "a").write("LLM_API_KEY=x")'),
        ("审批规则文件", 'open("run/sandbox_rules.json", "w").write("{}")'),
        ("原生调用", "import ctypes\nctypes.CDLL('kernel32')"),
        ("动态导入原生库", 'm = __import__("ctypes")'),
        ("不可解析", "def broken(:\n    pass"),
        ("空代码", "   \n"),
    ],
)
def test_hard_floor_hits(label: str, snippet: str, tmp_path: Path) -> None:
    reason = hard_floor(snippet, tmp_path)
    assert reason, f"硬底线漏了这一类：{label}"
    assert isinstance(reason, str) and reason.strip()


def test_hard_floor_blocks_deleting_the_workdir_or_its_parents(tmp_path: Path) -> None:
    """删工作目录本身、删它的上级（= 项目根）、删工作区外 —— 一律拒。"""
    workdir = tmp_path / "proj" / "exports"
    for target in (str(workdir), str(tmp_path / "proj"), str(Path.home() / "Desktop")):
        code = f"import shutil\nshutil.rmtree({target!r})"
        assert hard_floor(code, workdir), f"删 {target} 竟然没有命中硬底线"
        code = f"import os\nos.remove({target!r})"
        assert hard_floor(code, workdir), f"os.remove 删 {target} 竟然没有命中硬底线"


def test_hard_floor_allows_normal_edits(tmp_path: Path) -> None:
    """正常改文件的代码不该被硬底线挡住（否则这个工具就没法用了）。"""
    workdir = tmp_path / "exports"
    code = (
        "from docx import Document\n"
        "doc = Document('a.docx')\n"
        "doc.add_paragraph('新的一段')\n"
        "doc.save('a.docx')\n"
        "print('saved')\n"
    )
    assert hard_floor(code, workdir) is None


@pytest.mark.parametrize(
    "snippet",
    [
        'open(r"C:\\Windows\\win.ini", "w").write("x")',
        'open(".env", "a").write("x")',
        "import ctypes",
        "import shutil\nshutil.rmtree(%s)",
    ],
)
def test_no_choice_can_bypass_the_hard_floor(snippet: str, tmp_path: Path) -> None:
    """**任何选择都不能放行**：风险键全预授权 + 四个选项逐个试，全部 ``rejected``。"""
    workdir = tmp_path / "exports"
    if "%s" in snippet:
        snippet = snippet % str(workdir)
    gate = _gate(tmp_path)
    _grant_everything(gate, snippet, workdir)
    for approval in (None, *APPROVAL_LEVELS):
        decision = gate.check(snippet, workdir=workdir, session_id="s1", approval=approval)
        assert decision.kind == "rejected", (
            f"approval={approval!r} 竟然放行了硬底线样本：{decision.kind}"
        )
        assert decision.risks == [], "硬底线的判定不该给界面留「要不要允许」的空间"


# --------------------------------------------------------------------------- #
# 2. 四选项 + 3. 规则记忆（类别 × 路径范围）
# --------------------------------------------------------------------------- #


def test_no_approval_returns_needs_approval_with_four_options(tmp_path: Path) -> None:
    """第一次不带 approval：回 ``needs_approval``，把风险与四个选项交给界面。"""
    workdir = tmp_path / "exports"
    gate = _gate(tmp_path)
    decision = gate.check('open("a.txt", "w").write("x")', workdir=workdir, session_id="s1")
    assert decision.kind == "needs_approval"
    assert decision.pending, "必须告诉界面「还没被授权的是什么」"
    assert {item["value"] for item in approval_options()} == set(APPROVAL_LEVELS)
    assert [item["value"] for item in approval_options()] == ["deny", "once", "session", "always"]


def test_once_is_used_only_once(tmp_path: Path) -> None:
    """``once`` 只用一次：下次同样的代码还要重新问。"""
    workdir = tmp_path / "exports"
    gate = _gate(tmp_path)
    code = 'open("a.txt", "w").write("x")'
    assert gate.check(code, workdir=workdir, session_id="s1", approval="once").allowed
    again = gate.check(code, workdir=workdir, session_id="s1")
    assert again.kind == "needs_approval"
    assert gate.list_rules(session_id="s1") == {"always": [], "session": []}, "once 不该留痕"


def test_session_grant_covers_same_category_in_this_session_only(tmp_path: Path) -> None:
    """``session``：同一类别、同一范围在本会话内继续有效，换会话/换范围就失效，且不落盘。"""
    workdir = tmp_path / "exports"
    gate = _gate(tmp_path)
    assert gate.check(
        'open("a.txt", "w").write("x")', workdir=workdir, session_id="s1", approval="session"
    ).allowed
    # 同类同范围（哪怕换个文件名）→ 直接放行
    assert gate.check('open("b.txt", "w").write("y")', workdir=workdir, session_id="s1").allowed
    # 换个会话 → 还要再问
    assert (
        gate.check('open("b.txt", "w").write("y")', workdir=workdir, session_id="s2").kind
        == "needs_approval"
    )
    # 换个范围（子目录）→ 还要再问
    assert (
        gate.check('open("sub/b.txt", "w").write("y")', workdir=workdir, session_id="s1").kind
        == "needs_approval"
    )
    assert gate.store.load() == [], "session 级规则不该落盘"


def test_always_persists_and_covers_the_same_category(tmp_path: Path) -> None:
    """``always``：落盘，重启（新建一个门）后仍有效；只覆盖**同一类别 × 同一范围**。"""
    workdir = tmp_path / "exports"
    gate = _gate(tmp_path)
    assert gate.check(
        'open("out/a.docx", "w").write("x")', workdir=workdir, session_id="s1", approval="always"
    ).allowed
    stored = [rule.key for rule in gate.store.load()]
    assert "write:out/**" in stored and "exec:@cwd" in stored

    reopened = ApprovalGate(RuleStore(tmp_path / "run" / "sandbox_rules.json"))
    assert reopened.check(
        'open("out/b.docx", "w").write("y")', workdir=workdir, session_id="s9"
    ).allowed
    # 换范围 → 仍要问（规则是"类别 × 范围"，不是"这个工具随便用"）
    assert (
        reopened.check(
            'open("other/c.docx", "w").write("z")', workdir=workdir, session_id="s9"
        ).kind
        == "needs_approval"
    )
    # 换类别（写 → 删）→ 仍要问
    assert (
        reopened.check('import os\nos.remove("out/a.docx")', workdir=workdir, session_id="s9").kind
        == "needs_approval"
    )


def test_rule_store_lists_and_revokes(tmp_path: Path) -> None:
    """设置界面要能列、能撤（后端只做这两件事）。"""
    store = RuleStore(tmp_path / "run" / "sandbox_rules.json")
    store.grant("write:exports/**", level="always", origin="ses_1")
    store.grant("network:@any", level="always", origin="ses_1")
    assert [rule.key for rule in store.load()] == ["write:exports/**", "network:@any"]
    assert store.revoke("write:exports/**") is True
    assert [rule.key for rule in store.load()] == ["network:@any"]
    assert store.revoke("write:exports/**") is False, "撤不存在的规则要如实回 False"


def test_scope_matching_is_prefix_based() -> None:
    """规则范围按目录前缀覆盖：``write:out/**`` 管得住 ``out/sub/**``，管不住 ``other/**``。"""
    assert rule_covers("write:out/**", Risk("write", "out/**"))
    assert rule_covers("write:out/**", Risk("write", "out/sub/**"))
    assert not rule_covers("write:out/**", Risk("write", "other/**"))
    assert not rule_covers("write:out/**", Risk("delete", "out/**")), "类别不同不算覆盖"
    assert rule_covers("network:@any", Risk("network", "@any"))


def test_content_level_risk_cannot_be_made_permanent(tmp_path: Path) -> None:
    """内容级风险（网络 / 子进程 / 原生 / 工作区外路径 / 动态执行）的 ``always`` 降级为 ``session``。"""
    workdir = tmp_path / "exports"
    gate = _gate(tmp_path)
    decision = gate.check(
        "import socket\nprint(1)", workdir=workdir, session_id="s1", approval="always"
    )
    assert decision.allowed
    assert decision.downgraded is True
    assert "降级为本会话" in decision.reason
    assert gate.store.load() == [], "内容级风险不许写进永久规则"
    assert gate.check("import socket\nprint(2)", workdir=workdir, session_id="s1").allowed
    # 没被降级的普通写操作仍然可以永久
    plain = gate.check(
        'open("a.txt", "w").write("x")', workdir=workdir, session_id="s1", approval="always"
    )
    assert plain.downgraded is False
    # 只落了 ``write:@cwd``：``exec:@cwd`` 已经被上面那次降级的 session 授权覆盖，不必重复落盘
    assert [rule.key for rule in gate.store.load()] == ["write:@cwd"]


# --------------------------------------------------------------------------- #
# 5. 连拒熔断 + 拒绝文案
# --------------------------------------------------------------------------- #


def test_three_denials_trip_the_breaker(tmp_path: Path) -> None:
    """连续 3 次被拒 → 第 4 次（连问都不问）直接拒，且文案带"别换写法"。"""
    workdir = tmp_path / "exports"
    gate = _gate(tmp_path)
    code = 'open("a.txt", "w").write("x")'
    for index in range(MAX_CONSECUTIVE_DENIALS):
        decision = gate.check(code, workdir=workdir, session_id="s1", approval="deny")
        assert decision.kind == "rejected"
        assert decision.denial_streak == index + 1
        assert REFUSAL_TAIL in decision.reason
    tripped = gate.check(code, workdir=workdir, session_id="s1")
    assert tripped.kind == "rejected"
    assert "熔断" in tripped.reason
    assert REFUSAL_TAIL in tripped.reason
    # 换个会话不受影响（熔断是会话级的）
    assert gate.check(code, workdir=workdir, session_id="s2").kind == "needs_approval"


def test_successful_run_resets_the_streak(tmp_path: Path) -> None:
    """连续计数只在"成功放行"时清零：两次拒绝 + 一次授权 → 计数归零。"""
    workdir = tmp_path / "exports"
    gate = _gate(tmp_path)
    code = 'open("a.txt", "w").write("x")'
    gate.check(code, workdir=workdir, session_id="s1", approval="deny")
    gate.check(code, workdir=workdir, session_id="s1", approval="deny")
    gate.check(code, workdir=workdir, session_id="s1", approval="once")
    after = gate.check(code, workdir=workdir, session_id="s1", approval="deny")
    assert after.denial_streak == 1


def test_asking_for_approval_is_not_a_denial(tmp_path: Path) -> None:
    """``needs_approval`` 是"在等用户"，不能算一次拒绝（否则问三次就自己熔断）。"""
    workdir = tmp_path / "exports"
    gate = _gate(tmp_path)
    code = 'open("a.txt", "w").write("x")'
    for _ in range(MAX_CONSECUTIVE_DENIALS + 1):
        assert gate.check(code, workdir=workdir, session_id="s1").kind == "needs_approval"
    assert gate.check(code, workdir=workdir, session_id="s1", approval="once").allowed


def test_rejection_text_forbids_detours() -> None:
    """拒绝文案必须照搬那句话（"不要换写法重试，也不要换条路达成同一目的"）。"""
    assert REFUSAL_TAIL == "不要换写法重试，也不要换条路达成同一目的。"

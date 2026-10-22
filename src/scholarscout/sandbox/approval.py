"""``py.exec`` 的审批门：硬底线 → 风险分类 → 四选项授权 → 规则记忆 → 连拒熔断（docs/24 §4）。

一条调用要过五道，顺序不能换（换顺序就有绕过口子）：

1. **硬底线**（:func:`hard_floor`）：命中即拒，**任何授权都不能放行** —— 它在读规则之前执行，
   而且返回类型里没有"允许"这个值，只有"拒绝理由"或 ``None``。这是"无法绕过"的结构性证明：
   放行分支在代码里根本到不了硬底线命中之后的路径。
2. **熔断**：同一会话连续被拒 3 次后，第 4 次起连问都不问，直接拒（防模型反复试探）。
3. **风险分类**（:func:`classify`）：把代码折成「**类别 × 路径范围**」的风险清单，
   如 ``write:exports/**``、``network:@any``。规则记忆按这个维度存，不按代码字符串。
4. **规则匹配**：``always``（落盘）与 ``session``（内存）合并成一张已授权集合，全都覆盖才放行。
5. **四选项**：``deny`` / ``once`` / ``session`` / ``always``。其中
   **内容级风险（网络 / 子进程 / 原生调用 / 工作区外路径 / 动态执行）不许永久放行** ——
   这类范围不可穷举，"永远允许"会被降级成"本会话"（照搬 Hermes 的做法）。

**拒绝文案**统一带上 :data:`REFUSAL_TAIL`：模型被拒后换写法重试是最常见的浪费，
而"换条路达成同一目的"正是审批门要挡的事。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scholarscout.sandbox.rules import RuleStore

#: 拒绝时必须带的一句话（照搬 Hermes 第 5 条）
REFUSAL_TAIL = "不要换写法重试，也不要换条路达成同一目的。"
#: 连续被拒几次后熔断
MAX_CONSECUTIVE_DENIALS = 3
#: 四个选项（顺序即界面按钮顺序）
APPROVAL_LEVELS = ("deny", "once", "session", "always")
#: 内容级风险：范围不可穷举，``always`` 一律降级为 ``session``
CONTENT_RISK_CATEGORIES = frozenset({"network", "subprocess", "native", "outside_path", "dynamic"})

_NETWORK_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "urllib",
        "http",
        "httpx",
        "requests",
        "aiohttp",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "websockets",
        "paramiko",
    }
)
_PROCESS_MODULES = frozenset({"subprocess", "multiprocessing", "pty"})
_NATIVE_MODULES = frozenset({"ctypes", "cffi", "_ctypes"})
_DESTRUCTIVE_CALLS = frozenset({"rmtree", "remove", "unlink", "rmdir", "removedirs"})
_WRITE_CALLS = frozenset({"open", "write_text", "write_bytes"})
#: 系统位置：碰一下就拒（不区分读还是写 —— 这些位置本来就不该出现在 py.exec 里）
_SYSTEM_PREFIXES = (
    "c:/windows",
    "c:/program files",
    "c:/program files (x86)",
    "c:/programdata",
    "c:/$recycle.bin",
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/lib",
    "/var",
    "/dev",
)
_ENV_MARKERS = ("%windir%", "%systemroot%", "%programfiles%", "%programfiles(x86)%", "$env:windir")
_RULES_FILENAME = "sandbox_rules.json"
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class Risk:
    """一条风险：``category`` × ``scope`` 就是规则记忆的键。"""

    category: str
    scope: str
    detail: str = ""

    def key(self) -> str:
        """规则键，如 ``write:exports/**``。"""
        return f"{self.category}:{self.scope}"

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key(),
            "category": self.category,
            "scope": self.scope,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Decision:
    """审批门的判定：``allowed`` / ``needs_approval`` / ``rejected`` 三态。"""

    kind: str
    reason: str
    risks: list[Risk]
    pending: list[str]
    downgraded: bool = False
    denial_streak: int = 0

    @property
    def allowed(self) -> bool:
        return self.kind == "allowed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "risks": [risk.as_dict() for risk in self.risks],
            "pending": list(self.pending),
            "downgraded": self.downgraded,
            "denial_streak": self.denial_streak,
        }


def approval_options() -> list[dict[str, str]]:
    """界面上那四个按钮（文案与语义一一对应，前端照这个渲染）。"""
    return [
        {"value": "deny", "label": "不许执行"},
        {"value": "once", "label": "只允许这一次"},
        {"value": "session", "label": "本会话内允许同类操作"},
        {"value": "always", "label": "永远允许同类操作（内容级风险只到本会话）"},
    ]


# --------------------------------------------------------------------------- #
# 路径与 AST 小工具
# --------------------------------------------------------------------------- #


def _posix(path: Path) -> str:
    return path.as_posix()


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _resolve_literal(value: str, root: Path) -> Path | None:
    """把字面量路径按工作目录解析成规范绝对路径；解析不了返回 ``None``。"""
    try:
        candidate = Path(value).expanduser()
    except (OSError, ValueError):
        return None
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return candidate.resolve()
    except OSError:
        return None


def _looks_absolute(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    return bool(_DRIVE_PATH.match(text)) or text.startswith("/")


def _system_path_hit(value: str) -> bool:
    lowered = value.replace("\\", "/").lower()
    if any(marker in lowered for marker in _ENV_MARKERS):
        return True
    return any(lowered == prefix or lowered.startswith(prefix + "/") for prefix in _SYSTEM_PREFIXES)


def _string_literals(tree: ast.AST) -> list[str]:
    """代码里的字符串常量（**跳过模块 docstring**：说明文字里提到 ``C:\\Windows`` 不该被当代码）。"""
    skip: ast.AST | None = None
    body = getattr(tree, "body", None)
    if isinstance(body, list) and body:
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            skip = first.value
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node is not skip:
            found.append(node.value)
    return found


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.append(node.module)
            elif node.level:
                modules.append("." * node.level)
    return modules


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _literal_arg(
    node: ast.Call, *, names: tuple[str, ...] = ("path", "src", "dst", "name")
) -> str | None:
    """取调用的第一个「路径样」实参：位置参数 0，或指定关键字。"""
    candidates: list[ast.expr] = []
    if node.args:
        candidates.append(node.args[0])
    for keyword in node.keywords:
        if keyword.arg in names:
            candidates.append(keyword.value)
    for candidate in candidates:
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
            return candidate.value
    return None


def _mode_of(node: ast.Call) -> str | None:
    """``open`` 的模式串（第二位置参数或 ``mode=``）。"""
    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        value = node.args[1].value
        if isinstance(value, str):
            return value
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            if isinstance(value, str):
                return value
    return None


def _destructive_targets(tree: ast.AST) -> list[tuple[str, str]]:
    """删除类调用的``(函数名, 目标字面量)`` —— 只收字面量，变量留给运行时 audit hook。"""
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in _DESTRUCTIVE_CALLS:
            continue
        target = _literal_arg(node)
        if target is not None:
            found.append((name, target))
    return found


def _write_targets(tree: ast.AST) -> list[tuple[str, str, str]]:
    """写类调用的``(函数名, 目标字面量, 模式)``：``open(..., "w")`` / ``Path.write_text`` / ``.save(...)``。"""
    found: list[tuple[str, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "open":
            mode = _mode_of(node) or "r"
            if not any(flag in mode for flag in "wax+"):
                continue
            target = _literal_arg(node)
            if target is not None:
                found.append((name, target, mode))
            continue
        if name in _WRITE_CALLS or name in {"save", "save_as"}:
            target = _literal_arg(node, names=("path", "filename", "name", "dst"))
            if target is not None:
                found.append((name, target, "w"))
    return found


def _uses_dynamic_import(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) in {
            "__import__",
            "eval",
            "exec",
            "compile",
        }:
            return True
    return False


def _imports_native(tree: ast.AST) -> bool:
    for module in _imported_modules(tree):
        if module.split(".")[0] in _NATIVE_MODULES:
            return True
    # ``__import__("ctypes")`` 这种字符串形式：靠字面量兜住（不确定时偏拒绝）
    return any("ctypes" in literal for literal in _string_literals(tree))


def _scope_for(target: str, root: Path) -> str:
    """把目标路径折成规则范围：``@cwd`` / ``exports/**`` / ``outside:C:/Users/…``。"""
    resolved = _resolve_literal(target, root)
    if resolved is None:
        return "@any"
    if not _inside(resolved, root):
        return "outside:" + _posix(resolved.parent)
    relative = resolved.relative_to(root)
    parent = relative.parent.as_posix()
    if parent in ("", "."):
        return "@cwd"
    return parent + "/**"


def _split_key(key: str) -> tuple[str, str]:
    category, _, scope = key.partition(":")
    return category, scope


def rule_covers(rule_key: str, risk: Risk) -> bool:
    """规则键是否覆盖这条风险：类别必须相同，范围相等或规则范围是它的上级目录。"""
    category, scope = _split_key(rule_key)
    if category != risk.category:
        return False
    if scope in ("@any", risk.scope):
        return True
    base = scope.removesuffix("/**")
    target = risk.scope.removesuffix("/**")
    if base == target:
        return True
    return target.startswith(base + "/")


# --------------------------------------------------------------------------- #
# 硬底线
# --------------------------------------------------------------------------- #


def hard_floor(code: str, workdir: Path) -> str | None:
    """硬底线检查：返回拒绝理由，或 ``None``（= 没有命中，可以继续走审批）。

    **命中即终局**：调用方（:class:`ApprovalGate`）在它返回非 ``None`` 时**直接拒**，
    不读规则、不看 ``approval`` 参数。函数本身也没有"允许"这种返回值 ——
    这是"任何选择都不能放行"的代码级保证，而不是一句承诺。
    """
    root = Path(workdir).expanduser().resolve()
    text = (code or "").strip()
    if not text:
        return "代码是空的：没有可执行内容"
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return f"代码无法解析（第 {exc.lineno} 行：{exc.msg}）：不能在无法静态检查的情况下执行"

    for literal in _string_literals(tree):
        if _system_path_hit(literal):
            return f"代码里出现系统目录 {literal!r}：这些位置一律不许 py.exec 触碰"
        name = Path(literal.replace("\\", "/")).name.lower()
        if name == ".env" or name.endswith(".env"):
            return f"代码里出现凭据文件 {literal!r}：.env 一律不许 py.exec 读写"
        if name == _RULES_FILENAME:
            return "代码里出现审批规则文件：不允许 py.exec 给自己发许可证"

    if _imports_native(tree):
        return "代码涉及 ctypes/原生调用：那等价于任意代码执行，任何授权都不放行"

    for name, target in _destructive_targets(tree):
        resolved = _resolve_literal(target, root)
        if resolved is None:
            continue  # 静态说不清 → 交给运行时 audit hook（它按真实路径判）
        if resolved == root or resolved in root.parents:
            return f"{name}({target!r}) 指向工作目录本身或其上级：删除不可逆，一律不放行"
        if not _inside(resolved, root):
            return f"{name}({target!r}) 指向工作目录之外：删除不可逆，一律不放行"
    return None


# --------------------------------------------------------------------------- #
# 风险分类
# --------------------------------------------------------------------------- #


def classify(code: str, workdir: Path) -> list[Risk]:
    """把代码折成风险清单（按 ``category:scope`` 去重，保留首条说明）。"""
    root = Path(workdir).expanduser().resolve()
    risks: list[Risk] = [Risk("exec", "@cwd", "在工作目录里执行 Python")]
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return risks

    for module in _imported_modules(tree):
        top = module.split(".")[0]
        if top in _NETWORK_MODULES:
            risks.append(Risk("network", "@any", f"导入 {top}（可以出网）"))
        elif top in _PROCESS_MODULES:
            risks.append(Risk("subprocess", "@any", f"导入 {top}（可以起子进程）"))
        elif top in _NATIVE_MODULES:
            risks.append(Risk("native", "@any", f"导入 {top}（可以直接调原生）"))

    for literal in _string_literals(tree):
        if not _looks_absolute(literal):
            continue
        resolved = _resolve_literal(literal, root)
        if resolved is not None and not _inside(resolved, root):
            risks.append(
                Risk("outside_path", "outside:" + _posix(resolved.parent), f"绝对路径 {literal!r}")
            )

    for name, target, mode in _write_targets(tree):
        risks.append(Risk("write", _scope_for(target, root), f"{name}({target!r}, {mode})"))

    for name, target in _destructive_targets(tree):
        risks.append(Risk("delete", _scope_for(target, root), f"{name}({target!r})"))

    if _uses_dynamic_import(tree):
        risks.append(Risk("dynamic", "@any", "用 __import__/eval/exec/compile 动态执行"))

    unique: dict[str, Risk] = {}
    for risk in risks:
        unique.setdefault(risk.key(), risk)
    return list(unique.values())


# --------------------------------------------------------------------------- #
# 审批门
# --------------------------------------------------------------------------- #


class ApprovalGate:
    """四选项授权的状态机：``always`` 落盘、``session`` 在内存、``once`` 不留痕。"""

    def __init__(self, store: RuleStore | None = None) -> None:
        self.store = store if store is not None else RuleStore()
        #: 会话级授权：``{session: {"write:@cwd", ...}}`` —— 随进程消失，不落盘
        self._session: dict[str, set[str]] = {}
        #: 连续被拒计数（成功放行即清零）
        self._denials: dict[str, int] = {}

    # -- 规则视图（设置界面用） ------------------------------------------- #

    def list_rules(self, *, session_id: str | None = None) -> dict[str, list[dict[str, str]]]:
        """列出永久规则与本会话规则（两条来源分开给，界面不必自己合并）。"""
        session = self._session_key(session_id)
        return {
            "always": [rule.as_dict() for rule in self.store.load()],
            "session": [
                {"key": key, "level": "session"}
                for key in sorted(self._session.get(session, set()))
            ],
        }

    def revoke(self, key: str, *, session_id: str | None = None) -> bool:
        """撤销一条规则（永久与本会话都撤）；返回是否真的删掉了东西。"""
        session = self._session_key(session_id)
        removed = self.store.revoke(key)
        grants = self._session.get(session)
        if grants is not None and key.strip() in grants:
            grants.discard(key.strip())
            removed = True
        return removed

    # -- 判定 --------------------------------------------------------------- #

    def check(
        self,
        code: str,
        *,
        workdir: Path,
        session_id: str | None = None,
        approval: str | None = None,
    ) -> Decision:
        """判定这次执行能不能跑。**不抛**（参数非法除外）：拒也是正常结果。"""
        session = self._session_key(session_id)
        floor = hard_floor(code, workdir)
        if floor is not None:
            streak = self._bump(session)
            return Decision(
                kind="rejected",
                reason=floor + REFUSAL_TAIL,
                risks=[],
                pending=[],
                denial_streak=streak,
            )

        streak = self._denials.get(session, 0)
        if streak >= MAX_CONSECUTIVE_DENIALS:
            return Decision(
                kind="rejected",
                reason=(
                    f"本会话已连续 {streak} 次被拒，审批熔断：这类操作在本次会话内不再询问，直接拒绝。"
                    + REFUSAL_TAIL
                ),
                risks=[],
                pending=[],
                denial_streak=streak,
            )

        risks = classify(code, workdir)
        granted = set(self.store.keys()) | self._session.get(session, set())
        pending = [risk.key() for risk in risks if not self._covered(risk, granted)]
        if not pending:
            self._denials[session] = 0
            return Decision("allowed", "已授权的规则覆盖本次操作的全部范围", risks, [])

        if approval is None:
            keys = "、".join(pending)
            return Decision(
                kind="needs_approval",
                reason=f"这次执行要动到：{keys}。需要用户确认后才能跑。",
                risks=risks,
                pending=pending,
            )
        if approval not in APPROVAL_LEVELS:
            raise ValueError(f"未知的审批选项：{approval!r}（只认 {'/'.join(APPROVAL_LEVELS)}）")
        if approval == "deny":
            streak = self._bump(session)
            return Decision(
                kind="rejected",
                reason="用户没有允许这次执行。" + REFUSAL_TAIL,
                risks=risks,
                pending=pending,
                denial_streak=streak,
            )

        level = approval
        downgraded = False
        if level == "always" and any(risk.category in CONTENT_RISK_CATEGORIES for risk in risks):
            level = "session"
            downgraded = True
        if level == "session":
            self._session.setdefault(session, set()).update(pending)
        elif level == "always":
            for key in pending:
                self.store.grant(key, level="always", origin=session)
        self._denials[session] = 0
        label = {
            "once": "只允许这一次",
            "session": "本会话内允许同类操作",
            "always": "永远允许同类操作",
        }[level]
        reason = f"已按「{label}」放行（{len(pending)} 条风险）。"
        if downgraded:
            reason += "检测到内容级风险（网络/子进程/原生/工作区外路径/动态执行），不能永久放行，已降级为本会话。"
        return Decision("allowed", reason, risks, pending, downgraded=downgraded)

    # -- 内部 --------------------------------------------------------------- #

    def _covered(self, risk: Risk, granted: set[str]) -> bool:
        return any(rule_covers(key, risk) for key in granted)

    def _bump(self, session: str) -> int:
        streak = self._denials.get(session, 0) + 1
        self._denials[session] = streak
        return streak

    @staticmethod
    def _session_key(session_id: str | None) -> str:
        value = (session_id or "").strip()
        return value or "@local"


__all__ = [
    "APPROVAL_LEVELS",
    "CONTENT_RISK_CATEGORIES",
    "MAX_CONSECUTIVE_DENIALS",
    "REFUSAL_TAIL",
    "ApprovalGate",
    "Decision",
    "Risk",
    "approval_options",
    "classify",
    "hard_floor",
    "rule_covers",
]

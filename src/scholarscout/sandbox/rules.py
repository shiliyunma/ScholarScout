"""审批规则的落盘：``<home>/run/sandbox_rules.json``（docs/24 §4）。

**为什么不写进 `config.yaml`**：`config.yaml` 是**用户手写的声明式配置**（带注释、由人编辑），
而审批规则是**程序在运行中自己增删的状态** —— 让程序去改写用户手写的 YAML，会顺手吃掉注释与键序，
还会和"配置要人审"的规矩打架。落盘的归属很清楚：会随运行变化的东西放 `run/`（`run/state.db`、
`run/logs/` 已经在里面），`run/sandbox_rules.json` 与它们同级。

存的是「**操作类别 × 路径范围**」而不是代码字符串：

```json
{"version": 1, "rules": [{"key": "write:exports/**", "level": "always", "origin": "ses_xxx", "created_at": "..."}]}
```

为什么不存代码字符串：一模一样的 `doc.save("out/a.docx")` 的哈希在参数变一个字之后就变了（永远命中不了），
而"允许写 `exports/` 下的文件"这条意图在 `a.docx` → `b.docx` 时**本来就该继续有效**。
反过来说，按类别记也意味着"允许写 `exports/`"不会顺手允许"在 `exports/` 里起子进程" —— 类别是规则的一半。

``session`` 级规则**不落盘**（见 :mod:`scholarscout.sandbox.approval`）：本会话就该随进程消失，
写进磁盘再标一句"本会话"就是自欺。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scholarscout import paths

#: 规则文件名（落在 ``run/`` 下）
RULES_FILENAME = "sandbox_rules.json"
#: 文件格式版本（换格式时用来拒绝旧文件，而不是猜）
SCHEMA_VERSION = 1
#: 落盘只允许这两种级别：``once`` 是一次性的，本来就不该有记录
PERSISTED_LEVELS = ("session", "always")


@dataclass(frozen=True)
class Rule:
    """一条审批规则：``key`` 形如 ``write:exports/**``。"""

    key: str
    level: str
    origin: str = ""
    created_at: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "level": self.level,
            "origin": self.origin,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(raw: object) -> Rule | None:
        """宽容解析：形状不对的条目**丢掉**（坏一条不该让整张表作废）。"""
        if not isinstance(raw, dict):
            return None
        key = raw.get("key")
        level = raw.get("level")
        if not isinstance(key, str) or not key.strip():
            return None
        if level not in PERSISTED_LEVELS:
            return None
        origin = raw.get("origin")
        created = raw.get("created_at")
        return Rule(
            key=key.strip(),
            level=str(level),
            origin=origin if isinstance(origin, str) else "",
            created_at=created if isinstance(created, str) else "",
        )


def rules_path(root: Path | None = None) -> Path:
    """规则文件路径：``<home>/run/sandbox_rules.json``。"""
    return paths.run_dir(root) / RULES_FILENAME


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RuleStore:
    """规则文件的读写门面。**每次读都回磁盘** —— 设置界面上看到的就是文件里的真值。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else rules_path()

    def load(self) -> list[Rule]:
        """读规则。文件缺失 / 坏 JSON / 版本不认识 → 空表（不抛：审批门不能因为文件坏了就打不开）。"""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
            return []
        entries = raw.get("rules")
        if not isinstance(entries, list):
            return []
        rules: list[Rule] = []
        for entry in entries:
            rule = Rule.from_dict(entry)
            if rule is not None:
                rules.append(rule)
        return rules

    def keys(self) -> list[str]:
        """已授予**永久**权限的规则键（审批门用）。"""
        return [rule.key for rule in self.load() if rule.level == "always"]

    def grant(self, key: str, *, level: str = "always", origin: str = "") -> Rule:
        """新增/覆盖一条规则（同 key 覆盖级别与时间）。只接受落盘级别。"""
        if level not in PERSISTED_LEVELS:
            raise ValueError(f"不落盘的级别：{level!r}（once 不该写进规则表）")
        rule = Rule(key=key.strip(), level=level, origin=origin, created_at=_now())
        rules = [item for item in self.load() if item.key != rule.key]
        rules.append(rule)
        self._write(rules)
        return rule

    def revoke(self, key: str) -> bool:
        """撤销一条规则；返回是否真的删掉了东西（设置界面据此回"没这条"）。"""
        rules = self.load()
        kept = [item for item in rules if item.key != key.strip()]
        if len(kept) == len(rules):
            return False
        self._write(kept)
        return True

    def _write(self, rules: list[Rule]) -> None:
        """原子写：先写临时文件再 ``os.replace``，避免半个 JSON 把审批门卡住。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": SCHEMA_VERSION, "rules": [rule.as_dict() for rule in rules]}
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        handle, temp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".sandbox_rules-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temp_name, self.path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

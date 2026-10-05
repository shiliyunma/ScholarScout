"""提示词装载器（照 AIHOT：提示词即配置，版本 = 内容哈希）。

- 提示词外置在 ``prompts/*.md``；本模块只负责读取与拼装，不改内容
- 支持两种占位：``{{> 片段名}}`` 引入规则片段、``{{变量}}`` 填入运行时值
- ``prompt_version`` = **拼装后（变量未替换）文本** 的 ``sha256[:12]``
  —— 改任意片段或主体，版本必变；变量值不同但模板相同则版本相同（正确：变量属于输入，不属于版本）
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_INCLUDE = re.compile(r"\{\{>\s*([A-Za-z0-9_\-]+)\s*\}\}")
_VARIABLE = re.compile(r"\{\{\s*([A-Za-z0-9_\-]+)\s*\}\}")
_MAX_INCLUDE_DEPTH = 5


class PromptError(RuntimeError):
    """提示词缺失或模板有问题。这类错误必须早失败，不能静默降级。"""


@dataclass(frozen=True)
class PromptPack:
    directory: Path

    def raw(self, name: str) -> str:
        path = self.directory / f"{name}.md"
        if not path.is_file():
            raise PromptError(f"提示词文件不存在：{path}")
        return path.read_text(encoding="utf-8").strip()

    def compose(self, name: str, **variables: str) -> str:
        """把 ``name`` 主体与它引用的片段拼起来，再替换变量。"""
        body = self._expand(self.raw(name), depth=0)
        return self._substitute(body, variables, name)

    def version_of(self, name: str) -> str:
        """只按模板（含片段）算版本，不含变量值。"""
        body = self._expand(self.raw(name), depth=0)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]

    def available(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(path.stem for path in self.directory.glob("*.md"))

    # ---------------------------------------------------------------- 内部

    def _expand(self, text: str, *, depth: int) -> str:
        if depth > _MAX_INCLUDE_DEPTH:
            raise PromptError(f"片段嵌套超过 {_MAX_INCLUDE_DEPTH} 层，可能是循环引用")

        def replace(match: re.Match[str]) -> str:
            return self._expand(self.raw(match.group(1)), depth=depth + 1)

        expanded = _INCLUDE.sub(replace, text)
        return expanded.strip() if depth == 0 else expanded

    def _substitute(self, text: str, variables: dict[str, str], name: str) -> str:
        missing: list[str] = []

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in variables:
                missing.append(key)
                return match.group(0)
            return variables[key]

        rendered = _VARIABLE.sub(replace, text)
        if missing:
            raise PromptError(f"提示词 {name} 缺少变量：{sorted(set(missing))}")
        return rendered

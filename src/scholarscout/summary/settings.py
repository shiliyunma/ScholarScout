"""摘要流水线的可调参数（独立成文件，避免 config ↔ summary 循环导入）。

键名与 `config.example.yaml` 的 ``summary:`` 段一一对应，见 docs/04 §4。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PROMPTS_DIR = "prompts"


@dataclass(frozen=True)
class SummarySettings:
    prompts_dir: Path
    truncate_chars: int = 6000  # 送模型前的正文截断阈值（AIHOT 实测用 6000）
    max_text_chars: int = 60000  # 本地全文硬上限，超出记 documents.truncated=1
    summary_min_chars: int = 80
    summary_max_chars: int = 160
    max_sentences: int = 3
    guard_check_numbers: bool = True
    max_points: int = 4

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None, *, home: Path) -> SummarySettings:
        section = raw or {}
        prompts_dir = Path(str(section.get("prompts_dir", DEFAULT_PROMPTS_DIR)))
        if not prompts_dir.is_absolute():
            prompts_dir = home / prompts_dir
        return cls(
            prompts_dir=prompts_dir,
            truncate_chars=int(section.get("truncate_chars", 6000)),
            max_text_chars=int(section.get("max_text_chars", 60000)),
            summary_min_chars=int(section.get("summary_min_chars", 80)),
            summary_max_chars=int(section.get("summary_max_chars", 160)),
            max_sentences=int(section.get("max_sentences", 3)),
            guard_check_numbers=bool(section.get("guard_check_numbers", True)),
            max_points=int(section.get("max_points", 4)),
        )

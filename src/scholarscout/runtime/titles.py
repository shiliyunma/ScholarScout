"""用模型把用户的第一句话总结成会话标题 —— **在轮次开始前**做，标题马上出现。

## 为什么必须在轮次开始前（踩过的顺序问题）

用户消息一落库，`sessions.retitle_if_unnamed` 就会用**本地截断**给它起名。
那个名字看起来像真标题（不是占位符），于是模型总结出来的标题**再也写不进去**。
所以顺序只能是：先总结标题 → 再跑轮次（此时本地兜底不会触发）。

## 成本与失败口径

一次极小调用（输入几十 token、输出十几 token）。**任何失败都返回 `None`**，
由调用方保留本地截断兜底 —— 标题不值得让一轮对话失败，也不值得重试。
配置项 `generation.title_by_model`（默认开）可以关掉，关掉即回到本地截断。
"""

from __future__ import annotations

import re
from pathlib import Path

from scholarscout.providers.llm import LlmClient
from scholarscout.summary.prompts import PromptPack

#: 提示词名（`prompts/suggest-title.md`）
PROMPT_NAME = "suggest-title"
#: 标题长度上限（字符）：再长在左侧栏就显示不全了
TITLE_MAX_CHARS = 24
#: 模型自己判定"信息不足"时输出这个，等价于没起出名字
UNNAMED = "未命名"

_QUOTES = "「」『』\"'“”‘’《》"
_TRAILING = "。．.！!？?；;：:，,、…"


def prompt_version(prompts_dir: Path) -> str:
    """提示词版本 = 内容哈希（改一个字版本就变，便于追溯标题是哪版提示词起的）。"""
    return PromptPack(directory=prompts_dir).version_of(PROMPT_NAME)


def clean_title(raw: str, *, max_chars: int = TITLE_MAX_CHARS) -> str | None:
    """把模型输出收拾成能直接用的标题；收拾不出东西就返回 None。"""
    text = re.sub(r"\s+", " ", raw or "").strip()
    text = text.strip(_QUOTES).strip()
    while text and text[-1] in _TRAILING:
        text = text[:-1].strip()
    text = text.strip(_QUOTES).strip()
    if not text or text == UNNAMED:
        return None
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def suggest_title(
    llm: LlmClient,
    text: str,
    *,
    prompts_dir: Path,
    max_chars: int = TITLE_MAX_CHARS,
) -> str | None:
    """让模型总结标题；出错/信息不足都返回 None（调用方走本地兜底）。"""
    first_line = (text or "").strip()
    if not first_line:
        return None
    messages = []
    try:
        # 提示词读取也在 try 里：提示词文件缺失不该让这一轮对话失败
        prompt = PromptPack(directory=prompts_dir).raw(PROMPT_NAME)
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": first_line[:1000]},
        ]
        reply = llm.chat(messages, tools=None)
    except Exception:
        return None
    return clean_title(reply.content or "", max_chars=max_chars)

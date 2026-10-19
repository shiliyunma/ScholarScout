"""飞书渠道的「要文件」意图识别（纯规则，零 IO；docs/17 §3.12）。

## 它在哪一层

与 `channels/feishu.py` 同层（渠道层入口），**只做一件事**：把一条消息解析成
:class:`FileRequest`（要哪一类 / 关键词），或者干脆回 `None`。查库、导出文件、上传、发送
都不在这里 —— 它们在主进程那一侧的 `channel.send_file`（`rpc/channel_file.py`）。

所以本模块**不 import `rpc`、不读库、不出网、不碰 socket**：全是可单独断言的纯函数
（`tests/channels/test_feishu_file_request.py` 直接喂字符串）。

## 为什么用规则而不是工具

用户拍板：「如果可以发送文件，请开发发送文件的渠道」+「生成物和已入库的资料都应该可以发」。
做法是**在入站路径里用规则解析**，不新增模型可见的工具、不改提示词：模型那一侧的结构
（工具表 / 系统提示）一个字都不动 —— 这是个确定性动作，不需要模型参与。

三条原则：

1. **保守识别**：必须同时有"发送动词"和"文件类目标"，且整句很短（≤40 字）；
   「给我讲讲学习路线」「为什么要做知识地图」这类会被明确排除（不能让一句话术把问答劫走）。
2. **解析不到就不发**：调用方拿 `None` 就照常走问答，绝不猜一个文件发出去。
3. **权限沿用入站拦截**：文件请求排在 `authorize_message` 之后 —— 未授权的人发不出东西来
   （否则"发我笔记"就是一条越权读取通道）。

## 与 feishu.py 的关系

从 `channels/feishu.py` 原样搬出（**只搬位置、没改函数体**）；`feishu.py` 仍重导出
`FileRequest` / `parse_file_request`，所以 `from scholarscout.channels.feishu import X` 照旧可用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- 「发文件」意图（规则识别，不新增工具）
#
# 用户拍板：「如果可以发送文件，请开发发送文件的渠道」+「生成物和已入库的资料都应该可以发」。
# 做法是**在入站路径里用规则解析**，不新增模型可见的工具、不改提示词：
# 模型那一侧的结构（工具表 / 系统提示）一个字都不动，`chat.send` 也不会被调用 ——
# 这是个确定性动作（查库 → 上传 → 发送），不需要模型参与，反而更省 token、更不会瞎发。
#
# 三条原则：
# 1. **保守识别**：必须同时有"发送动词"和"文件类目标"，且整句很短（≤40 字）；
#    「给我讲讲学习路线」「为什么要做知识地图」这类会被明确排除（不能让一句话术把问答劫走）。
# 2. **解析不到就不发**：回可读提示 + 候选清单，绝不猜一个文件发出去。
# 3. **权限沿用入站拦截**：文件请求排在 `authorize_message` 之后 —— 未授权的人发不出东西来
#    （否则"发我笔记"就是一条越权读取通道）。

#: 发送类动词（用户原话：「发我 / 给我 / 传我 …文件/文档/PDF/路线/笔记/地图」）
FILE_VERB_RE = re.compile(
    r"(发我|发给我|给我发|发一下|发个|发一份|发份|传我|传给我|发送|导出|来一份|来份|给我)"
)
#: 动词后面紧跟这些词的，是**提问**不是"要文件"（「给我讲讲」「发我解释一下」）
FILE_ASK_RE = re.compile(
    r"^(讲|说|解释|介绍|分析|看看|看一下|读|对比|总结|列|写|做|画|查|搜|算|评|推荐)"
)
#: 目标里必须出现的"文件类"线索（按类别判归属）
FILE_ARTIFACT_HINTS: tuple[tuple[str, str], ...] = (
    (r"地图|主题图|思维导图", "topic"),
    (r"路线|计划|学习安排", "route"),
    (r"笔记|卡片|抽背", "note"),
)
FILE_LIBRARY_RE = re.compile(r"文件|文档|资料|论文|文献|文章|pdf|附件", re.IGNORECASE)
#: 本地路径线索（`data/…`、`D:\…`、`xxx.md`）—— 只认这两种形态，不做"猜路径"
FILE_PATH_RE = re.compile(
    r"(^[A-Za-z]:[\\/]|^data[\\/]|^run[\\/]|[\w\-.]+\.(md|pdf|txt|csv|json|docx?|pptx?|xlsx?)$)",
    re.IGNORECASE,
)
#: 目标里的填充词（"发我**那份**论文" → "论文"）：清掉后剩下的才是搜索词
FILE_FILLER_RE = re.compile(
    r"(一份|那份|这份|这个|那个|那篇|这篇|上次的|刚才的|我的|的|文件|文档|资料|论文|文献|文章|pdf|md|markdown|附件)",
    re.IGNORECASE,
)
#: 句首填充词（"请/帮我/麻烦/把/能不能…"）—— 动词前后的修饰语
FILE_LEAD_RE = re.compile(r"^(请|帮我|帮忙|麻烦|能不能|能|可以|你|再|把|将|我想要|我要|\s)+")
#: 尾部的客套话（"发我路线，谢谢"）：先剥掉，再判"这是不是一句话"
FILE_POLITE_RE = re.compile(r"[，,]\s*(谢谢|多谢|麻烦了|拜托|好吗|可以吗|行吗)[！!。.～~]*$")
#: 目标里出现这些标点 = 它是一句话/一段话，**不是一个可以发的目标**
#: （实例：「发我一下这篇文章的主要贡献，顺便说说它的局限…」——38 字，长度护栏拦不住）
FILE_SENTENCE_RE = re.compile(r"[，,；;！!？?]")


@dataclass(frozen=True)
class FileRequest:
    """一次"要文件"的解析结果（纯数据，可单独断言）。

    `kind`：`artifact`（本会话/库里最近的生成物）/ `library`（已入库资料）/ `path`（本地路径）。
    `query`：`artifact` 时是 `topic|route|note` 或空串（=最近的那件）；`library` 时是标题模糊词；
    `path` 时是用户给的相对/绝对路径。
    """

    kind: str
    query: str = ""
    raw: str = ""


def _strip_lead(text: str) -> str:
    """反复剥掉句首填充词（一次替换可能只剥掉"请帮我"里的"请"）。"""
    result = text.strip()
    for _ in range(3):
        trimmed = FILE_LEAD_RE.sub("", result).strip()
        if trimmed == result:
            break
        result = trimmed
    return result


def _looks_like_target(text: str) -> bool:
    """像不像"一个可以发的目标"：路径 / 生成物 / 入库资料，三类线索之一。"""
    if not text:
        return False
    if FILE_PATH_RE.search(text):
        return True
    if any(re.search(pattern, text) for pattern, _ in FILE_ARTIFACT_HINTS):
        return True
    return bool(FILE_LIBRARY_RE.search(text))


def _classify(target: str) -> FileRequest:
    """把目标短语归到三类之一（顺序即优先级：路径 > 生成物 > 入库资料 > 最近生成物）。"""
    clean = target.strip().strip("。！？!?，,、~～ ")
    if FILE_PATH_RE.search(clean):
        return FileRequest("path", clean.strip("\"'“”"), target)
    for pattern, name in FILE_ARTIFACT_HINTS:
        if re.search(pattern, clean):
            return FileRequest("artifact", name, target)
    if FILE_LIBRARY_RE.search(clean):
        query = re.sub(r"\s+", " ", FILE_FILLER_RE.sub(" ", clean)).strip()
        return FileRequest("library", query, target)
    # 只有动词没有名词（"发我文件"/"发我一份"）：给**最近的那件生成物**
    query = re.sub(r"\s+", " ", FILE_FILLER_RE.sub(" ", clean)).strip()
    return FileRequest("artifact", "", query)


def parse_file_request(text: str) -> FileRequest | None:
    """判断一条消息是不是"给我发某个文件"，是就解析出目标（纯函数、零 IO）。

    能识别的两种语序：
    - **动词在前**：`发我学习路线` / `传我那份 PDF` / `给我 注意力机制 那篇论文`；
    - **目标在前**：`把学习路线发我` / `学习路线发我一下`。

    识别失败（提问、闲聊、超长）一律回 `None` —— 调用方据此走正常问答，**绝不瞎发**。
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 40:
        return None  # 一整段话里夹一句"发我"不算指令（长度是最便宜的护栏）
    body = raw.rstrip("。.!！~～ ")
    match = FILE_VERB_RE.search(body)
    if match is None:
        return None
    head = _strip_lead(body[: match.start()].strip())
    tail = body[match.end() :].strip()
    tail_core = _strip_lead(tail)
    if tail_core:
        if FILE_ASK_RE.match(tail_core):
            return None  # 「给我讲讲学习路线」——这是提问，不是要文件
        if FILE_SENTENCE_RE.search(FILE_POLITE_RE.sub("", tail_core)):
            return None  # 一整句话（有逗号/问号）= 在提问/闲聊，不是在点名一个文件
        if _looks_like_target(tail_core):
            return _classify(tail_core)
        return None
    # 动词在句末：「把学习路线发我」——目标在动词之前
    if head and _looks_like_target(head):
        if FILE_SENTENCE_RE.search(head):
            return None
        return _classify(head)
    return None

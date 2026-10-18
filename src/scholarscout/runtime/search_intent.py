"""轻量搜索意图分类 + 每轮检索偏好位（方案 3 的**后端**）。

## 为什么要有它（以及为什么不动工具表/不动系统提示）

用户遇到的真问题：模型拿着一句模糊的"帮我找点资料"，默认把 arxiv + github + 通用检索全查一遍，
既费 token 又常答非所问。改法有两条路：**拆工具**（检索论文/检索代码各一个工具）或**先分类再筛来源**。
拆工具会改工具声明与系统提示 → ``system_prompt_hash`` 变 → 历史回答与旧报告立刻不可比；
所以这一步只做**内部行为**：工具签名、描述、系统提示一字不动，只在 ``research.search`` 执行前
读一个"偏好位"，据此筛 provider 或干脆不搜。

## 三件事

1. **规则预筛（免费）**：用户明说"只看论文 / 只要代码 / 别搜了"→ 直接定偏好，不调模型；
   只有**模糊查询**（太短、没有领域词、泛指"资料/了解一下"）才值得花一次小调用。
2. **分类器（一次极小调用）**：提示词在 ``prompts/classify-search-intent.md``，走 ``chat()``
   （不是 ``chat_stream``）、**无工具**、``max_tokens`` 压到几十；结果为一行 JSON。
   **失败开放**：报错 / 超时 / 解析不出 → 当 ``other`` 继续检索，绝不阻断（分类是省钱的辅助，
   不是检索的前置条件）。
3. **每轮检索偏好位**：``paper|code|tutorial|news|data|other|none``，存在研究台账
   （``research_states.payload_json``）里。**不进系统提示**：系统提示是静态文本的哈希
   （``runtime/prompts.build_system_prompt``），偏好位每轮都会变；混进去会让
   ``system_prompt_hash`` 随用户点选变化，"这份回答出自哪一版提示词"就再也说不清。

## 类型词表与 ``fetch/urls.guess_source_type`` 的对应

``paper`` ≈ ``arxiv``；``code`` ≈ ``github``；``tutorial`` / ``news`` / ``data`` ≈ ``official`` 与 ``web``；
``other`` = 不筛，全量。两处不是同一个枚举（一个描述"要找什么"，一个描述"抓回来的 URL 是什么"），
所以这里只做**来源族映射**（见 :data:`PROVIDER_FAMILIES`），不强行合并。

## 四种路径（``research.search`` 据此行事，见 tools/research_search.py）

| 情况 | 行为 |
|---|---|
| 有偏好（非 none） | 只查该族的 provider；该族没有可用来源就如实回"没来源"而不是退回去查别的 |
| 无偏好 + 分类置信度高 | 同上（高置信会直接写成偏好位） |
| 无偏好 + 分类置信度低 | **不检索**，回 ``needs_choice`` + 选项，并发 ``chat.needs_choice`` 事件给界面 |
| 分类器报错/超时/解析失败 | 当 ``other`` **照常检索**（``failed=True`` 只在台账里留痕） |
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from scholarscout.providers.llm import LlmClient
from scholarscout.summary.prompts import PromptPack

#: 可选类型（与 ``prompts/classify-search-intent.md`` 的类型表一字对应）
TYPES: tuple[str, ...] = ("paper", "code", "tutorial", "news", "data", "other")
#: 偏好位允许的取值：六类 + ``none``（用户明确表示本轮不检索）
PREFERENCES: tuple[str, ...] = (*TYPES, "none")
#: 高于等于它才算"敢按类型筛来源"；低置信一律先问用户（宁可多问一句，不要搜错一整轮）
CONFIDENCE_THRESHOLD = 0.6
#: 分类调用的输出上限（只输出一行 JSON，几十个 token 足够）
MAX_TOKENS = 64
#: 喂给分类器的查询截断（分类只需要"要找什么"，不需要整段粘贴）
MAX_QUERY_CHARS = 300
#: 归一化查询的 LRU 容量（256 条 ≈ 一次会话里的全部不同说法）
CACHE_SIZE = 256
#: 提示词名（``prompts/classify-search-intent.md``）
PROMPT_NAME = "classify-search-intent"

#: 研究台账里的三个键。**只落库、不渲染进任何提示词**（``render_note`` 不认识它们）
PREFERENCE_KEY = "search_preference"
PREFERENCE_TURN_KEY = "search_preference_turn"
INTENT_KEY = "search_intent"

#: 来源族：偏好 → 该查哪些 provider（``other`` 不在表里 = 全量）
PAPER_PROVIDERS: tuple[str, ...] = ("arxiv", "openalex", "semantic_scholar")
CODE_PROVIDERS: tuple[str, ...] = ("github",)
WEB_PROVIDERS: tuple[str, ...] = (
    "searxng",
    "tavily",
    "serper",
    "brave",
    "google_cse",
    "bing",
    "duckduckgo",
)
PROVIDER_FAMILIES: dict[str, tuple[str, ...]] = {
    "paper": PAPER_PROVIDERS,
    "code": CODE_PROVIDERS,
    "tutorial": WEB_PROVIDERS,
    "news": WEB_PROVIDERS,
    "data": WEB_PROVIDERS,
}

#: 用户**明说**偏好时的说法（免费规则，命中就不调模型）。顺序即优先级：先说"不检索"。
_EXPLICIT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "none",
        re.compile(
            r"(?:不要|不用|别|先别|无需|不需要|先不)\s*(?:再)?\s*(?:检索|搜索|搜|查)"
            r"|直接回答|不检索|不用检索|别检索"
        ),
    ),
    (
        "paper",
        re.compile(
            r"(?:只想?看|只要|仅想?看|仅要|只给|就给|只查|限定为?|就要)\s*(?:论文|文献|paper|预印本)"
        ),
    ),
    (
        "code",
        re.compile(
            r"(?:只想?看|只要|仅想?看|仅要|只给|就给|只查|限定为?|就要)\s*(?:代码|实现|仓库|repo|github)"
        ),
    ),
    (
        "tutorial",
        re.compile(
            r"(?:只想?看|只要|仅想?看|仅要|只给|就给|只查|限定为?|就要)\s*(?:教程|文档|tutorial|课程)"
        ),
    ),
    (
        "news",
        re.compile(
            r"(?:只想?看|只要|仅想?看|仅要|只给|就给|只查|限定为?|就要)\s*(?:新闻|动态|news)"
        ),
    ),
    (
        "data",
        re.compile(
            r"(?:只想?看|只要|仅想?看|仅要|只给|就给|只查|限定为?|就要)\s*(?:数据|数据集|榜单|benchmark)"
        ),
    ),
    ("other", re.compile(r"都可以|都行|全部来源|所有来源|不限来源|照旧搜|正常搜")),
)

#: 出现了这些线索就**不必问模型**：用户已经说了要哪种形式（中英都认，归一化后比较）
_DOMAIN_HINTS: tuple[str, ...] = (
    "论文",
    "文献",
    "预印本",
    "期刊",
    "综述",
    "paper",
    "arxiv",
    "代码",
    "仓库",
    "实现",
    "复现",
    "github",
    "教程",
    "文档",
    "视频",
    "课程",
    "tutorial",
    "文档",
    "新闻",
    "动态",
    "发布",
    "数据",
    "dataset",
    "benchmark",
    "榜单",
    "评测",
)

#: 泛指词：说了这些等于没说（"帮我找点资料"给不出类型，值得问一次）
_GENERIC_WORDS: tuple[str, ...] = (
    "资料",
    "东西",
    "了解",
    "入门",
    "看看",
    "调研",
    "研究",
    "学习",
    "推荐",
    "相关",
    "介绍",
    "怎么学",
)

_JSON_RE = re.compile(r"\{[^{}]*\}")


@dataclass(frozen=True)
class Intent:
    """一次判定结果。

    ``source`` 说明它是**怎么来的**（``explicit`` 规则 / ``model`` 模型 / ``cache`` 命中 /
    ``fallback`` 失败开放），``failed`` 只在这种"没判出来"时为真 —— 调用方据此决定
    "照常检索"还是"低置信先问用户"，两者绝不能混。
    """

    type: str
    confidence: float
    source: str
    failed: bool = False
    usage: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "confidence": round(float(self.confidence), 3),
            "source": self.source,
            "failed": bool(self.failed),
        }


# ------------------------------------------------------------------ 规则部分


def normalize(text: str) -> str:
    """归一化：去空白与标点、转小写（与 ``research_state.normalize_query`` 同一口径）。"""
    return re.sub(r"[\s\W_]+", "", str(text or "").lower())


def label_of(preference: str | None) -> str:
    """偏好 → 给人看的中文标签（没偏好回空串）。"""
    for item in options():
        if item["type"] == preference:
            return str(item["label"])
    return ""


def explicit_preference(text: str) -> str | None:
    """用户明说的偏好（"只看论文" / "别搜了"）；没说返回 ``None``。"""
    compact = re.sub(r"\s+", "", str(text or ""))
    if not compact:
        return None
    lowered = compact.lower()
    for preference, pattern in _EXPLICIT_RULES:
        if pattern.search(lowered):
            return preference
    return None


def looks_ambiguous(text: str) -> bool:
    """这句查询值不值得问一次模型？

    只在"判不出来"时才 true —— 这一条是**成本闸门**：每命中一次就多一次极小模型调用。
    """
    query = normalize(text)
    if not query:
        return False
    if any(hint in query for hint in _DOMAIN_HINTS):
        return False  # 已经说了要什么形式，不需要问
    if len(query) <= 3:
        return True  # 太短，判不出来
    if any(word in query for word in _GENERIC_WORDS):
        return True  # 泛指（"找点资料/了解一下"）
    return len(query) <= 10  # 短查询且没有任何类型线索


def providers_for(preference: str | None) -> tuple[str, ...] | None:
    """偏好 → 该族的 provider；``None`` 表示**不筛**（全量）。"""
    if preference is None:
        return None
    return PROVIDER_FAMILIES.get(preference)


def filter_providers(
    wanted: list[str], available: dict[str, Any], preference: str | None
) -> list[str]:
    """按偏好筛 ``wanted``（保序）；点了名但类型不符的来源会被丢掉。

    族内一个可用来源都没有时返回空表 —— 调用方据此如实回"这个类型现在没有可用来源"，
    **不要**偷偷退回全量（那等于没听懂用户）。
    """
    family = providers_for(preference)
    if family is None:
        return wanted
    picked = [name for name in wanted if name in family]
    if picked:
        return picked
    # 模型点名了别的族（或配置里默认来源不在这一族）：按族内可用来源补
    return [name for name in family if name in available]


def options() -> list[dict[str, Any]]:
    """给界面/模型看的选项卡（下一步的选择卡直接用这份）。"""
    return [
        {"type": "paper", "label": "论文", "hint": "arXiv / OpenAlex 的预印本与期刊"},
        {"type": "code", "label": "代码", "hint": "GitHub 仓库与可复现实现"},
        {"type": "tutorial", "label": "教程", "hint": "入门指南 / 官方文档 / 视频课"},
        {"type": "news", "label": "新闻", "hint": "发布与更新动态"},
        {"type": "data", "label": "数据", "hint": "数据集 / benchmark / 榜单"},
        {"type": "other", "label": "都看看", "hint": "不筛，全部来源都查"},
        {"type": "none", "label": "先不检索", "hint": "直接用已有资料回答"},
    ]


# ------------------------------------------------------------------ 分类器


#: 归一化查询 → 判定（小 LRU：同一句话在一轮里可能被问多次，别重复花 token）
_CACHE: OrderedDict[str, Intent] = OrderedDict()


def clear_cache() -> None:
    """清空缓存（测试用；产品里没有这个入口）。"""
    _CACHE.clear()


def cache_size() -> int:
    return len(_CACHE)


def _remember(key: str, intent: Intent) -> None:
    _CACHE[key] = intent
    _CACHE.move_to_end(key)
    while len(_CACHE) > CACHE_SIZE:
        _CACHE.popitem(last=False)


def parse_intent(raw: str) -> Intent | None:
    """从模型输出里抠出判定；抠不出**或类型不认识**都返回 ``None``（那是失败，不是 other）。"""
    match = _JSON_RE.search(str(raw or ""))
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("type") or "").strip().lower()
    if kind not in TYPES:
        return None
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    return Intent(kind, max(0.0, min(1.0, confidence)), "model")


def _prompts_dir(prompts_dir: Path | None) -> Path:
    if prompts_dir is not None:
        return Path(prompts_dir)
    # src/scholarscout/runtime/search_intent.py → 仓库根/prompts
    return Path(__file__).resolve().parents[3] / "prompts"


def _chat_bounded(llm: LlmClient, messages: list[dict[str, Any]]) -> Any:
    """调一次模型：无工具、输出上限压小（客户端支持 per-call 覆盖时才传）。"""
    chat: Any = llm.chat
    if getattr(llm, "supports_max_tokens", False):
        return chat(messages, tools=None, max_tokens=MAX_TOKENS)
    return chat(messages, tools=None)


def classify(
    llm: LlmClient,
    text: str,
    *,
    prompts_dir: Path | None = None,
    use_cache: bool = True,
) -> Intent:
    """判定这句话要哪类资料。**任何失败都返回 ``failed=True`` 的 other，绝不抛异常。**"""
    query = str(text or "").strip()
    if not query:
        return Intent("other", 0.0, "fallback", failed=True)
    key = normalize(query)[:200]
    if use_cache:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return replace(hit, source="cache")
    try:
        # 提示词读取也在 try 里：文件缺失不该让检索失败
        prompt = PromptPack(directory=_prompts_dir(prompts_dir)).raw(PROMPT_NAME)
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": query[:MAX_QUERY_CHARS]},
        ]
        reply = _chat_bounded(llm, messages)
    except Exception:
        return Intent("other", 0.0, "fallback", failed=True)
    parsed = parse_intent(getattr(reply, "content", "") or "")
    if parsed is None:
        return Intent("other", 0.0, "fallback", failed=True)
    usage = {
        k: int(v or 0)
        for k, v in (getattr(reply, "usage", None) or {}).items()
        if isinstance(v, int)
    }
    intent = replace(parsed, usage=usage)
    if use_cache:
        _remember(key, intent)
    return intent


# ------------------------------------------------------------------ 偏好位


def set_preference(
    state: dict[str, Any],
    *,
    preference: str | None,
    turn_id: str | None,
    intent: Intent | None = None,
) -> None:
    """把偏好位写进**台账 dict**（落库由调用方做，写者只有一个：``runtime/loop.py``）。"""
    if preference is not None and preference not in PREFERENCES:
        raise ValueError(f"未知的检索偏好：{preference!r}（允许：{'、'.join(PREFERENCES)}）")
    if preference is None:
        # 没有偏好就不留键：留着上一轮的值会让"每轮"变成"每会话"，读到旧值还会搜错
        state.pop(PREFERENCE_KEY, None)
        state.pop(PREFERENCE_TURN_KEY, None)
    else:
        state[PREFERENCE_KEY] = preference
        state[PREFERENCE_TURN_KEY] = turn_id
    if intent is not None:
        state[INTENT_KEY] = {**intent.as_dict(), "turn_id": turn_id}


def intent_of(value: Any) -> Intent | None:
    """把台账里存的 dict 还原成 :class:`Intent`；坏数据回 ``None``（不猜）。"""
    if not isinstance(value, dict):
        return None
    kind = str(value.get("type") or "").strip().lower()
    if kind not in TYPES:
        return None
    try:
        confidence = float(value.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return None
    return Intent(
        kind,
        max(0.0, min(1.0, confidence)),
        str(value.get("source") or "model"),
        bool(value.get("failed")),
    )


def preference_for(ctx: Any) -> tuple[str | None, Intent | None]:
    """读本轮偏好位 → ``(preference, intent)``。

    两条来源，显式覆盖优先：

    1. ``ctx.extra[PREFERENCE_KEY]`` —— 调用方直接给的（CLI/测试/界面点选后透传）；
    2. 研究台账 —— 轮次开始时 :func:`resolve_turn` 写的那一份；**只认本轮的 intent**
       （上一轮的置信度不能拿来决定这一轮要不要问用户）。
    """
    from scholarscout.runtime import research_state

    extra = getattr(ctx, "extra", None) or {}
    if PREFERENCE_KEY in extra:
        preference = extra.get(PREFERENCE_KEY)
        if preference not in PREFERENCES and preference is not None:
            preference = None
        return preference, intent_of(extra.get(INTENT_KEY))
    session_id = getattr(ctx, "session_id", None)
    if not session_id:
        return None, None
    state = research_state.load_state(ctx.db, session_id) or {}
    preference = state.get(PREFERENCE_KEY)
    if preference not in PREFERENCES:
        preference = None
    record = state.get(INTENT_KEY)
    intent = intent_of(record)
    # 不是本轮判的，就不能用它的置信度决定"要不要问"
    if (
        intent is not None
        and isinstance(record, dict)
        and record.get("turn_id") != getattr(ctx, "turn_id", None)
    ):
        intent = None
    return preference, intent


def resolve_turn(
    llm: LlmClient | None,
    text: str,
    *,
    state: dict[str, Any],
    turn_id: str | None,
    prompts_dir: Path | None = None,
    enabled: bool = True,
) -> Intent | None:
    """轮次开始时确定本轮偏好位（``runtime/loop.py`` 调用，直接改 ``state``）。

    - 关掉开关 → 什么都不做（完全回到引入前的行为）；
    - 用户明说 → 规则直接定，**不调模型**；
    - 模糊查询 → 一次极小调用；高置信才写偏好位，低置信只留 intent（让工具去问用户）；
    - 查询本身很明确（说了论文/代码/教程…）→ 不动已有偏好位，也不调模型。

    返回本次判定（没判定则为 ``None``）；调用方据此记账 ``usage_records.purpose="intent"``。
    """
    if not enabled:
        return None
    explicit = explicit_preference(text)
    if explicit is not None:
        intent = Intent(explicit, 1.0, "explicit")
        set_preference(state, preference=explicit, turn_id=turn_id, intent=intent)
        return intent
    if llm is None or not looks_ambiguous(text):
        return None
    intent = classify(llm, text, prompts_dir=prompts_dir)
    if intent.failed:
        # 失败开放：保留已有偏好位不变，也不写新偏好 —— 检索照常按原样走
        set_preference(state, preference=state.get(PREFERENCE_KEY), turn_id=turn_id, intent=intent)
        return intent
    if intent.confidence >= CONFIDENCE_THRESHOLD:
        set_preference(state, preference=intent.type, turn_id=turn_id, intent=intent)
    else:
        set_preference(state, preference=state.get(PREFERENCE_KEY), turn_id=turn_id, intent=intent)
    return intent

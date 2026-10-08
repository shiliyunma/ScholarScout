"""学习模块的对外门面：实现按域拆进 learning_topics / learning_routes / learning_cards。

为什么保留这个文件：外部（rpc 层、测试）一律 `from scholarscout.state.learning import X`，
把实现搬走但**入口名不动**，改动就不会扩散到调用方 —— 与 rpc/methods.py 的做法一致。
"""

from __future__ import annotations

# 笔记、抽背卡与复习
from scholarscout.state.learning_cards import (
    CARD_TYPE_CHOICE,
    CARD_TYPE_FILL,
    CARD_TYPE_SHORT,
    _clean_cards,
    _has_content,
    card_review_map,
    card_stats,
    due_cards,
    get_note,
    grade_card,
    list_notes,
    notes_for_source,
    parse_cards,
    replay_card,
    reveal_card,
    review_card,
    save_note,
)

# 学习路线与版本历史
from scholarscout.state.learning_routes import (
    archive_version,
    get_route,
    get_version,
    latest_route,
    list_routes,
    list_versions,
    revise_route,
    save_route,
)

# 主题地图
from scholarscout.state.learning_topics import (
    get_topic_map,
    latest_topic_map,
    list_topic_maps,
    save_topic_map,
)

__all__ = [
    "CARD_TYPE_CHOICE",
    "CARD_TYPE_FILL",
    "CARD_TYPE_SHORT",
    "_clean_cards",
    "_has_content",
    "archive_version",
    "card_review_map",
    "card_stats",
    "due_cards",
    "get_note",
    "get_route",
    "get_topic_map",
    "get_version",
    "grade_card",
    "latest_route",
    "latest_topic_map",
    "list_notes",
    "list_routes",
    "list_topic_maps",
    "list_versions",
    "notes_for_source",
    "parse_cards",
    "replay_card",
    "reveal_card",
    "review_card",
    "revise_route",
    "save_note",
    "save_route",
    "save_topic_map",
]

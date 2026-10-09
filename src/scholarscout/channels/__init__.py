"""渠道层（入口）：把外部消息汇成一次会话（方案见 `docs/17` §3）。

和 `desktop/` **同级**：都是"前门"（外面来一条消息 → 跑一轮 → 把回复送回去）。
因此它**只经 `rpc/` 说话**，不读库、不出网、不 import `runtime/tools/state/providers/...`
—— 这条由 `tests/test_layering.py::RULES["channels"]` 钉死（违反就当场红）。

当前只有一个渠道：`feishu`（把 Hermes 的飞书回调做法拍扁进本项目）。
"""

from __future__ import annotations

__all__: list[str] = []

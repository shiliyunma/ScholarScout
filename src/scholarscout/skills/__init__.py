"""本地技能库（``skills/`` 目录）的索引层：只读元数据，供 ``skill.list`` / ``skill.load`` 用。

**为什么单独一个包**：索引要能被工具层、RPC 层与桌面 UI 同时读（下一轮的 ``/`` 菜单直接渲染它），
而它自己不该知道任何工具/协议/界面的事 —— 分层规则见 ``tests/test_layering.py``。
"""

from __future__ import annotations

__all__: list[str] = []

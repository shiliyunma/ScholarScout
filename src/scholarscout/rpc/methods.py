"""RPC 方法表（与 docs/01 §3 一一对应）。

**这是注册入口**：方法实现按域拆在同目录的模块里，import 它们就会触发注册。
对外名字保持不变（`AppContext` / `dispatch` / `method_names` 仍从这里 import），
所以调用方与测试不需要改任何一行。
"""

from __future__ import annotations

from scholarscout.rpc import (  # noqa: F401
    archive,
    channel,
    channel_file,
    feishu,
    learning,
    research,
    sandbox,
    session,
    settings,
)
from scholarscout.rpc.registry import (
    AppContext,
    dispatch,
    method,
    method_names,
)

__all__ = ["AppContext", "dispatch", "method", "method_names"]

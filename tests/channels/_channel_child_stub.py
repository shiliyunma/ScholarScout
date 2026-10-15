"""**桩渠道子进程**：给 supervisor 的起停测试当真子进程用（不装 SDK、不联网）。

它干的事与真子进程**同形**（照 `channels/runner.py`）：

1. 用 `channels/remote.py` 的 `StdioClient` 在 stdin/stdout 上跟父进程说话；
2. **真的发一次 RPC**（`channel.list`）并把结果写进状态快照的 `rpc_probe` 字段 ——
   这样测试就能证明"这条管道真通了"，而不是只看进程活着；
3. 日志只走 stderr（stdout 是协议通道）；
4. 之后挂在 stdin 上等父进程 kill（真子进程挂在长连接上，形状一致）。

为什么不用"sleep 一个进程"当桩：那样测不出 RPC 桥，也测不出状态快照。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scholarscout.channels import remote, supervisor  # noqa: E402


def main() -> int:
    platform = sys.argv[1] if len(sys.argv) > 1 else "feishu"
    client = remote.StdioClient()
    try:
        listing = client.request("channel.list", {})
    except Exception as exc:  # pragma: no cover - 通道坏了就让测试看到
        print(f"[stub] rpc failed: {exc}", file=sys.stderr, flush=True)
        return 3
    channels = listing.get("channels") if isinstance(listing, dict) else None
    count = len(channels) if isinstance(channels, list) else -1
    supervisor.write_state(
        platform,
        {
            "state": "running",
            "connected": True,
            "pid": os.getpid(),
            "rpc_probe": f"channels={count}",
            "stdout_clean": True,
        },
    )
    print(f"[stub] rpc ok channels={count} pid={os.getpid()}", file=sys.stderr, flush=True)
    # 把收到的每一行都记进 stderr（父进程推来的通知也走这里），然后一直挂着等被 kill
    print(json.dumps({"ready": True}), file=sys.stderr, flush=True)
    for _line in sys.stdin:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover - 由 pytest 以子进程方式起
    sys.exit(main())

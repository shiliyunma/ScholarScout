"""stdio 编码回归：管道里的中文/emoji 必须走 UTF-8（发布包 exe 的真缺陷）。

真机现场（PyInstaller onedir 包，`--windowed`，管道喂中文）：

- **写结果那一侧**：`_chat_repl` 的 `print(f"助手> {text}")` 在模型回了 👋 时
  `UnicodeEncodeError: 'gbk' codec can't encode character '\\U0001f44b'`，整轮 `stop_reason=error`；
- **读输入那一侧**：UTF-8 字节被按 GBK 解，非法字节经 `surrogateescape` 变成孤立代理，
  一进 JSON 请求体 / UTF-8 日志文件就是 `UnicodeEncodeError: '\\udcb1' … surrogates not allowed`。

为什么源码运行看不出来：开发机上 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8` 由解释器读环境变量，
而 PyInstaller 引导器自己构造解释器配置，这两个变量**对冻结进程不生效**（实测：显式设了
`PYTHONIOENCODING=utf-8` 打出来的仍是 cp936 字节）。所以这里既测 `stdio` 的纯函数，
也**真的用一个 cp936 的管道跑一遍 CLI/RPC** —— 不模拟，靠 `PYTHONIOENCODING=cp936` 复现现场。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scholarscout.rpc.methods import AppContext
from scholarscout.rpc.server import serve
from scholarscout.stdio import force_utf8_stdio, force_utf8_stream, stdio_encodings

ROOT = Path(__file__).resolve().parents[1]


def _gbk_pipe() -> tuple[io.BytesIO, io.TextIOWrapper]:
    """一条像 cp936 管道的文本流（Windows 上 `subprocess.PIPE` 的真实现就是这样）。"""
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, encoding="cp936", errors="surrogateescape", newline="")


# --------------------------------------------------------------------------- #
# 1. 纯函数：真流改成 UTF-8，替身一概不动
# --------------------------------------------------------------------------- #


def test_force_utf8_stream_turns_a_cp936_pipe_into_utf8() -> None:
    """cp936 的流改成 UTF-8 之后，emoji 写得出去（修的就是这条崩溃）。"""
    raw, stream = _gbk_pipe()

    assert force_utf8_stream(stream) is True
    assert stream.encoding == "utf-8"

    stream.write("你好👋\n")
    stream.flush()

    assert raw.getvalue().decode("utf-8") == "你好👋\n"


def test_force_utf8_stream_falls_back_when_errors_kwarg_is_rejected() -> None:
    """只认 `encoding` 的实现（老解释器/替身）不许抛：退化成只改编码。"""

    class OnlyEncoding(io.TextIOWrapper):
        def reconfigure(self, **kwargs: Any) -> None:
            if "errors" in kwargs:
                raise TypeError("reconfigure() got an unexpected keyword argument 'errors'")
            super().reconfigure(**kwargs)

    raw = io.BytesIO()
    stream = OnlyEncoding(raw, encoding="cp936")

    assert force_utf8_stream(stream) is True
    assert stream.encoding == "utf-8"


@pytest.mark.parametrize(
    "stream",
    [
        None,  # 打包版 windowed 进程没有控制台时 sys.stdout 就是 None
        io.StringIO(),  # 测试替身：没有 reconfigure
        iter(["一行"]),  # serve() 收进来的 iter(lines)
    ],
)
def test_force_utf8_stream_leaves_non_standard_streams_alone(stream: Any) -> None:
    """不是 `io.TextIOWrapper` 就原样放过（返回 False），绝不 AttributeError。"""
    assert force_utf8_stream(stream) is False


def test_force_utf8_stdio_reports_the_live_encodings() -> None:
    """入口调的那一条：跑完三条流都报 utf-8（或测试替身的 none），且不抛。"""
    force_utf8_stdio()
    assert set(stdio_encodings()) == {"stdin", "stdout", "stderr"}


# --------------------------------------------------------------------------- #
# 2. RPC 帧：传给 serve() 的 cp936 流也会被改成 UTF-8
# --------------------------------------------------------------------------- #


def test_serve_writes_utf8_frames_even_when_the_stream_is_cp936(ctx: AppContext) -> None:
    """`serve()` 自己补一次重设 —— 别的调用方绕过入口时也回不到 cp936。

    断言的是**字节**：covers 的是"帧按 UTF-8 写出、emoji 不炸"这件事，
    旧实现会在工作线程里抛 UnicodeEncodeError，客户端一行响应都收不到。
    """
    raw, stream = _gbk_pipe()
    request = {
        "jsonrpc": "2.0",
        "id": "你好👋",
        "method": "echo.anything",
        "params": {"text": "中文与 emoji 👋"},
    }

    serve(
        ctx,
        stdin=iter([json.dumps(request, ensure_ascii=False) + "\n"]),
        stdout=stream,
        dispatcher=lambda _ctx, _method, params: params,
    )
    stream.flush()

    payload = json.loads(raw.getvalue().decode("utf-8"))
    assert payload["result"]["text"] == "中文与 emoji 👋"
    assert payload["id"] == "你好👋"


# --------------------------------------------------------------------------- #
# 3. 真进程：cp936 环境下用管道跑一遍 CLI（现场复现条件）
# --------------------------------------------------------------------------- #


def test_cli_rpc_pipe_keeps_chinese_and_emoji_under_a_cp936_environment(
    tmp_path: Path,
) -> None:
    """`PYTHONIOENCODING=cp936` + 管道 → 输出仍是 UTF-8、回显里中文与 emoji 原样。

    为什么不用 `capsys`：这条缺陷只在**真进程的真管道**上出现（替换成 StringIO 就绕过了
    编码层）。`SCHOLARSCOUT_HOME` 指到 tmp，库里/日志里都不碰仓库。
    """
    env = {
        **os.environ,
        "PYTHONUTF8": "0",
        "PYTHONIOENCODING": "cp936",
        "SCHOLARSCOUT_HOME": str(tmp_path),
    }
    env.pop("PYTHONPATH", None)
    request = {"jsonrpc": "2.0", "id": "你好👋", "method": "no.such.method"}

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "scout.py"), "--rpc"],
        input=(json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"),
        capture_output=True,
        env=env,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )

    stderr = proc.stderr.decode("utf-8", "replace")
    assert "UnicodeEncodeError" not in stderr
    # 旧实现这里全是 cp936 字节：用 utf-8 解会炸 —— 解码失败本身就是回归信号
    payload = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
    assert payload["id"] == "你好👋"
    assert "未知方法" in payload["error"]["message"]

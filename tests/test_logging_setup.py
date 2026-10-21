"""`logging_setup`：落盘位置 / 幂等 / 失败降级 / session_id 上下文（P0-1）。

为什么这四条必须有：日志落盘是**为故障现场服务**的功能，"写进去了""没重复写""写不进去也不崩"
"能认出是哪一轮"这四件事任何一条坏了，等功能真正要用的时候（用户说没反应）才发现，就已经晚了。
"""

from __future__ import annotations

import logging
import logging.handlers
from collections.abc import Iterator
from pathlib import Path

import pytest

from scholarscout import logging_setup, paths


@pytest.fixture(autouse=True)
def clean_root() -> Iterator[None]:
    """用例前后都摘掉本模块挂的 handler。

    root logger 是**进程级**的：别的用例（例如 `test_cli_defaults` 模拟 frozen 双击）会真的把
    handler 挂上去，留下来就会让"路径应该在哪"这类断言看到上一轮的现场。
    """
    root = logging.getLogger()
    level = root.level
    _drop_own_handlers(root)
    try:
        yield
    finally:
        _drop_own_handlers(root)
        root.setLevel(level)


def _drop_own_handlers(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if logging_setup.HANDLER_MARK in handler.__dict__:
            root.removeHandler(handler)
            handler.close()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`SCS_HOME` 语义用项目自己的环境变量名指到临时目录（`paths.home()` 认它）。"""
    monkeypatch.setenv("SCHOLARSCOUT_HOME", str(tmp_path))
    monkeypatch.delenv(logging_setup.ENV_LEVEL, raising=False)
    return tmp_path


def test_writes_a_line_into_run_logs_app_log(isolated_home: Path) -> None:
    target = logging_setup.setup_logging()

    assert target == isolated_home / "run" / "logs" / "app.log"
    assert target == paths.app_log_path()
    assert target.is_file()

    logging.getLogger("scs.test").info("落盘第一行")

    text = target.read_text(encoding="utf-8")
    assert "落盘第一行" in text
    # 格式：时间 + 级别 + logger 名 + 消息（缺一个，日志就只能靠猜是谁写的）
    assert "INFO" in text
    assert "scs.test" in text
    assert text.startswith("20")


def test_rotation_parameters_are_10mb_by_3(isolated_home: Path) -> None:
    logging_setup.setup_logging()

    handler = logging_setup._own(logging.getLogger(), logging_setup._KIND_FILE)
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 3
    # UTF-8 落盘：中文消息不许变成 "?" 或 raise（Windows 默认代码页是 GBK）
    assert handler.encoding is not None
    assert handler.encoding.lower() == "utf-8"


def test_second_call_does_not_duplicate_handlers_or_lines(isolated_home: Path) -> None:
    first = logging_setup.setup_logging()
    handlers = len(logging.getLogger().handlers)

    second = logging_setup.setup_logging()

    assert second == first
    assert len(logging.getLogger().handlers) == handlers, "重复调用不许再挂 handler"

    logging.getLogger("scs.test").warning("只应出现一次")
    assert first.read_text(encoding="utf-8").count("只应出现一次") == 1, "同一行不许写两遍"


def test_unusable_log_dir_degrades_to_stderr_without_raising(
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`run/` 被一个**普通文件**占住 → 建目录必失败；此时不许抛，控制台仍要有输出。"""
    (isolated_home / "run").write_text("我是文件，不是目录", encoding="utf-8")

    assert logging_setup.setup_logging() is None

    logging.getLogger("scs.test").error("降级后仍然看得见")
    captured = capsys.readouterr()
    assert "降级后仍然看得见" in captured.err
    assert "日志文件不可用" in captured.err, "降级这件事本身也得留个原因"


def test_session_and_turn_ids_are_stamped_per_turn(isolated_home: Path) -> None:
    target = logging_setup.setup_logging()
    logger = logging.getLogger("scs.turn")

    reset = logging_setup.bind_turn("ses_0001", "trn_0001")
    try:
        logger.info("带上下文的一行")
    finally:
        reset()

    lines = target.read_text(encoding="utf-8").splitlines()
    inside = next(line for line in lines if "带上下文的一行" in line)
    assert "sid=ses_0001" in inside
    assert "turn=trn_0001" in inside

    logger.info("清掉上下文后的一行")
    lines = target.read_text(encoding="utf-8").splitlines()
    outside = next(line for line in lines if "清掉上下文后的一行" in line)
    assert "sid=-" in outside, "上下文清掉后不该留着上一轮的身份"
    assert "ses_0001" not in outside

"""`logging_setup`：落盘 / 轮转 / 密钥脱敏 / 幂等 / 配置校验（P0 日志管道）。

为什么这几条必须有：日志落盘是**为故障现场服务**的功能。任何一条坏了（没写进去、写完重复、
密钥跟着写进去了、轮转没留历史、配置写了没生效），等功能真正要用的时候（用户说"没反应"）
才发现，就已经晚了 —— 而那一刻恰好无法复现。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from scholarscout import logging_setup, paths

#: 假密钥：**故意不带 `sk-` 前缀**，这样"真值替换"那道才被单独考到
#: （带前缀的串会被兜底正则一起拦住，测不出第一道到底有没有工作）。
FAKE_LLM_KEY = "fake-llm-key-7d41c2ab9f"

#: 另一枚假密钥，给"兜底正则"用例当"已配置的真实值"（只为让过滤器被装上）
FAKE_FEISHU_SECRET = "fake-feishu-secret-0b93ee51"

#: 命中兜底正则的形状（不是任何已配置的值）
SK_SHAPED = "sk-proj-9f8e7d6c5b4a3210"


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
    """`SCHOLARSCOUT_HOME` 语义用项目自己的环境变量名指到临时目录（`paths.home()` 认它）。

    顺带把**这台机器上真实存在的**密钥类环境变量全摘掉（`*_API_KEY` / `*_SECRET` / `*_TOKEN` …）：
    否则"配了哪几枚密钥"会随开发机的环境漂移，脱敏用例就成了看运气的用例。
    每个用例自己 `setenv` 它需要的那几枚。
    """
    monkeypatch.setenv("SCHOLARSCOUT_HOME", str(tmp_path))
    monkeypatch.delenv(logging_setup.ENV_LEVEL, raising=False)
    for name in list(os.environ):
        if logging_setup._is_secret_name(name):
            monkeypatch.delenv(name, raising=False)
    assert logging_setup.secret_values() == (), "夹具没摘干净：真实密钥变量还在环境里"
    return tmp_path


def _write_config(home: Path, body: str) -> None:
    (home / "config.yaml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------- 落盘 + 格式


def test_writes_a_line_into_run_logs_scout_log(isolated_home: Path) -> None:
    target = logging_setup.setup_logging()

    assert target == isolated_home / "run" / "logs" / "scout.log"
    assert target == paths.scout_log_path()
    assert target.is_file()

    logging.getLogger("scs.test").info("落盘第一行")

    text = target.read_text(encoding="utf-8")
    assert "落盘第一行" in text
    # 格式：时间 + 级别 + 模块名 + 消息（缺一个，日志就只能靠猜是谁写的）
    assert "INFO" in text
    assert "scs.test" in text
    assert text.startswith("20")


def test_level_filters_debug_out_when_level_is_info(isolated_home: Path) -> None:
    """默认 INFO：`DEBUG` 行**不许**落盘（否则日志被调试细节淹掉，现场反而不好读）。"""
    target = logging_setup.setup_logging(level="INFO")

    logger = logging.getLogger("scs.level")
    logger.debug("这行不该出现")
    logger.info("这行该出现")

    text = target.read_text(encoding="utf-8")
    assert "这行该出现" in text
    assert "这行不该出现" not in text


# ------------------------------------------------------------------- 轮转参数


def test_rotation_parameters_are_5mb_by_3(isolated_home: Path) -> None:
    logging_setup.setup_logging()

    handler = logging_setup._own(logging.getLogger(), logging_setup._KIND_FILE)
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 3
    # UTF-8 落盘：中文消息不许变成 "?" 或 raise（Windows 默认代码页是 GBK）
    assert handler.encoding is not None
    assert handler.encoding.lower() == "utf-8"


def test_rotation_creates_numbered_backup(isolated_home: Path) -> None:
    """用小 `max_mb` 把文件写满 → 必须真的滚出 `scout.log.1`（历史现场就是靠它）。"""
    target = logging_setup.setup_logging(max_mb=0.02, backups=3)
    assert target is not None

    logger = logging.getLogger("scs.rotate")
    for index in range(400):
        logger.info("轮转填充行 %s %s", index, "x" * 200)

    backup = target.with_name(target.name + ".1")
    assert backup.is_file(), "写满后没有生成 .1 备份 —— 轮转没生效"
    assert backup.stat().st_size > 0


# ------------------------------------------------------------------- 密钥脱敏


def test_real_secret_values_never_reach_the_file(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配了真实密钥 → 哪怕业务代码把值打进日志，盘上也只允许出现 `[REDACTED]`。"""
    monkeypatch.setenv("LLM_API_KEY", FAKE_LLM_KEY)
    target = logging_setup.setup_logging()
    assert target is not None

    logging.getLogger("scs.secret").error("调用模型失败 key=%s", FAKE_LLM_KEY)

    text = target.read_text(encoding="utf-8")
    assert FAKE_LLM_KEY not in text, "真实密钥被写进了日志文件（这道是主力，必须有）"
    assert logging_setup.REDACTED in text


def test_fallback_regex_redacts_sk_shaped_tokens(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sk-` 形状的串即使**不是**已配置的值（用户粘贴/新来源），也不许整串入盘。"""
    monkeypatch.setenv("FEISHU_APP_SECRET", FAKE_FEISHU_SECRET)
    target = logging_setup.setup_logging()
    assert target is not None

    logging.getLogger("scs.secret").warning("请求头 Authorization: Bearer %s", SK_SHAPED)

    text = target.read_text(encoding="utf-8")
    assert SK_SHAPED not in text, "兜底正则没拦住 sk- 形状的密钥"
    assert logging_setup.REDACTED in text


def test_no_redacting_filter_when_nothing_is_configured(isolated_home: Path) -> None:
    """一枚密钥都没配 → 不装脱敏过滤器（装了等于宣称"已脱敏"，其实没有）。"""
    logging_setup.setup_logging()

    handler = logging_setup._own(logging.getLogger(), logging_setup._KIND_FILE)
    assert handler is not None
    assert not any(isinstance(item, logging_setup.RedactingFilter) for item in handler.filters)


def test_redactor_prefers_the_longer_secret() -> None:
    """短值是长值前缀时，必须先换长的：否则盘上会剩下「[REDACTED] + 后半截明文」。"""
    redactor = logging_setup.RedactingFilter(["abcdefgh1234", "abcdefgh12345678"])

    assert redactor.redact("key=abcdefgh12345678") == "key=[REDACTED]"


# --------------------------------------------------------- 幂等 / 控制台保留


def test_second_call_does_not_duplicate_handlers_or_lines(isolated_home: Path) -> None:
    first = logging_setup.setup_logging()
    handlers = len(logging.getLogger().handlers)

    second = logging_setup.setup_logging()

    assert second == first
    assert len(logging.getLogger().handlers) == handlers, "重复调用不许再挂 handler"

    logging.getLogger("scs.test").warning("只应出现一次")
    assert first is not None
    assert first.read_text(encoding="utf-8").count("只应出现一次") == 1, "同一行不许写两遍"


def test_console_stream_is_kept_but_not_written_twice(
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """stderr 保留（开发时看得见），但**文件里只有一份**：控制台不是"再写一遍文件内容"。"""
    target = logging_setup.setup_logging()
    assert target is not None

    logging.getLogger("scs.test").warning("控制台也要看见")

    captured = capsys.readouterr()
    assert "控制台也要看见" in captured.err
    assert target.read_text(encoding="utf-8").count("控制台也要看见") == 1


# ------------------------------------------------------------------- 配置驱动


def test_config_log_keys_are_used(isolated_home: Path) -> None:
    """YAML 里写的三个键必须**真的生效**（"写了不读"是这个项目踩过的老坑）。"""
    _write_config(
        isolated_home,
        "runtime:\n  log_level: DEBUG\n  log_max_mb: 0.02\n  log_backups: 5\n",
    )

    target = logging_setup.setup_logging()

    assert target == isolated_home / "run" / "logs" / "scout.log"
    handler = logging_setup._own(logging.getLogger(), logging_setup._KIND_FILE)
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == int(0.02 * 1024 * 1024), "log_max_mb 没被读到"
    assert handler.backupCount == 5, "log_backups 没被读到"

    logging.getLogger("scs.cfg").debug("DEBUG 级别下这条要落盘")
    assert "DEBUG 级别下这条要落盘" in target.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("snippet", "needle"),
    [
        ("log_max_mb: 0", "log_max_mb"),
        ("log_max_mb: -3", "log_max_mb"),
        ("log_max_mb: 很大", "log_max_mb"),
        ("log_backups: 0", "log_backups"),
        ("log_backups: -1", "log_backups"),
        ("log_backups: 三份", "log_backups"),
        ("log_level: 详细", "log_level"),
    ],
)
def test_illegal_log_config_raises(
    isolated_home: Path,
    snippet: str,
    needle: str,
) -> None:
    """非法值**必须报错**，不许静默回默认（静默 = 用户以为改了、其实没改）。"""
    _write_config(isolated_home, f"runtime:\n  {snippet}\n")

    with pytest.raises(ValueError, match=needle):
        logging_setup.setup_logging()


# ------------------------------------------------------------- 失败降级 + 上下文


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
    assert target is not None
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


def test_log_path_is_derived_from_home(isolated_home: Path) -> None:
    """路径拼接只走 `paths`：换 home 就换目录，别在这里自己拼字符串。"""
    other = isolated_home / "elsewhere"

    assert logging_setup.log_path() == paths.log_dir() / "scout.log"
    assert logging_setup.log_path(other) == other / "run" / "logs" / "scout.log"

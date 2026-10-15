"""配置读取的守卫：`RuntimeProfile` / `LibraryProfile` 的每个字段都必须真能从 YAML 读出来。

**为什么要有这条**：`load_config` 里是**手写字段列表**，加了新字段却忘了接一行，
YAML 里写什么都会被**静默忽略** —— 用户以为开了，实际没开。
这不是假设：`runtime.plan_by_model` 就这么白白失效过一次
（真机跑完两轮才发现"子问题 0 条"，因为读取处没这一行）。
所以这里用 `dataclasses.fields` **枚举**，以后加字段必须同步接进读取处。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from scholarscout.config import LibraryProfile, RuntimeProfile, load_config
from scholarscout.fetch.settings import (
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_EXTRACT_TIMEOUT_S,
    DEFAULT_MINERU_POLL_TIMEOUT_S,
    DEFAULT_ROBOTS_TIMEOUT_S,
    FetchSettings,
)

#: 每个字段给一个**与默认值不同**的值，能区分"读到了"与"用了默认"
NON_DEFAULT_VALUES: dict[str, object] = {
    "max_tool_calls_per_turn": 7,
    "max_context_chars": 12345,
    "stream": False,
    "allow_clarify": False,
    "max_history_chars": 4321,
    "plan_by_model": True,
    # D3：加字段就必须同步这里，否则 YAML 里写这个键会被静默忽略（守卫测试会当场失败）
    "compact_after_chars": 5555,
    # 子 Agent P0：并行抓取的并发上限（fetch_many 读它）
    "max_parallel_fetches": 5,
    # 子 Agent P1：顺序子任务总开关（默认关；开着会多花模型调用）
    "subtask_enabled": True,
    # 飞书出站发送器开关（把回答推回飞书）。默认关。
    # 入站（websocket 长连接）没有配置键：不需要公网地址/回调/隧道，启动入口见 scripts/feishu_ws_up.py。
    "feishu_reply_enabled": True,
    # 飞书入站即时回执文案（docs/17 §3.11）：空串 = 关掉回执；默认是"已接受消息，正在处理中"。
    "feishu_ack_text": "别急，正在处理",
}


def _yaml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def test_every_runtime_field_is_wired_to_a_yaml_key(tmp_path: Path) -> None:
    fields = {field.name for field in dataclasses.fields(RuntimeProfile)}
    assert fields == set(NON_DEFAULT_VALUES), (
        "RuntimeProfile 加了/删了字段：请同步 NON_DEFAULT_VALUES 与 load_config 的读取处，"
        "否则 YAML 里写这个键会被静默忽略"
    )


def test_runtime_values_are_actually_read(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    body = "runtime:\n" + "".join(
        f"  {key}: {_yaml_value(value)}\n" for key, value in NON_DEFAULT_VALUES.items()
    )
    config_path.write_text(body, encoding="utf-8")

    config = load_config(config_path, load_env=False)

    for key, expected in NON_DEFAULT_VALUES.items():
        actual = getattr(config.runtime, key)
        assert actual == expected, f"runtime.{key} 没被读到（YAML 写了 {expected}，实际 {actual}）"
        assert type(actual) is type(expected), f"runtime.{key} 的类型应保持 {type(expected)}"


def test_defaults_hold_when_section_is_absent(tmp_path: Path) -> None:
    """没有 runtime 段时全用默认值 —— 而且要能一眼看出"默认是关的"。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("search:\n  providers: ['auto']\n", encoding="utf-8")
    config = load_config(config_path, load_env=False)
    assert config.runtime == RuntimeProfile()
    assert config.runtime.plan_by_model is False


# ------------------------------------------------------------------ `library:` 段
#: 资料落地布局（docs/19 §2）。**加字段就必须同步这里**，否则 YAML 里写 `library.<字段>`
#: 会被静默忽略（与上面 runtime 同一类失败）。
LIBRARY_NON_DEFAULT: dict[str, object] = {"layout": "collected/format"}


def test_every_library_field_is_wired_to_a_yaml_key() -> None:
    fields = {field.name for field in dataclasses.fields(LibraryProfile)}
    assert fields == set(LIBRARY_NON_DEFAULT), (
        "LibraryProfile 加了/删了字段：请同步 LIBRARY_NON_DEFAULT 与 load_config 的读取处"
    )


def test_library_layout_is_actually_read(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    body = "library:\n" + "".join(
        f"  {key}: {_yaml_value(value)}\n" for key, value in LIBRARY_NON_DEFAULT.items()
    )
    config_path.write_text(body, encoding="utf-8")

    config = load_config(config_path, load_env=False)

    assert config.library.layout == ("collected", "format")


def test_library_layout_defaults_to_session_then_format(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("search:\n  providers: ['auto']\n", encoding="utf-8")
    assert LibraryProfile().layout == ("session", "format")
    assert load_config(config_path, load_env=False).library.layout == ("session", "format")


def test_library_layout_rejects_unknown_segment(tmp_path: Path) -> None:
    """未知段**直接报错**：静默回默认 = 用户以为改了布局、其实还写在老位置。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("library:\n  layout: session/banana\n", encoding="utf-8")
    with pytest.raises(ValueError, match="未知的布局段"):
        load_config(config_path, load_env=False)


# ------------------------------------------------------------------ `fetch:` 段
#: 抓取参数（与 `config.example.yaml` 的 `fetch:` 段一一对应）。**加字段就必须同步这里**，
#: 否则 YAML 里写 `fetch.<字段>` 会被静默忽略（与 runtime / library 同一类失败）。
#: 其中五个是 2026-10-12 新增的**分阶段超时**（connect / deadline / robots / extract /
#: mineru_poll）—— 此前所有阶段共用一个 `timeout_s` 又被 `retries` 叠乘，真机单次抓取 486s。
FETCH_NON_DEFAULT: dict[str, object] = {
    "user_agent": "test-agent/9.9 (walk)",
    "timeout_s": 21.0,
    "connect_timeout_s": 1.5,
    "deadline_s": 42.5,
    "robots_timeout_s": 2.5,
    "retries": 4,
    "max_bytes": 1_234_567,
    "cache_ttl_hours": 1.5,
    "respect_robots": False,
    "robots_exempt_hosts": ("example.com",),
    "large_file_mb": 7.5,
    "extract_timeout_s": 3.25,
    "mineru_poll_timeout_s": 12.5,
}


def test_every_fetch_field_is_wired_to_a_yaml_key() -> None:
    fields = {field.name for field in dataclasses.fields(FetchSettings)}
    assert fields == set(FETCH_NON_DEFAULT), (
        "FetchSettings 加了/删了字段：请同步 FETCH_NON_DEFAULT 与 FetchSettings.from_raw 的读取处"
    )


def _fetch_yaml_value(value: object) -> str:
    """写 YAML 时多认一个 tuple：`_yaml_value` 只处理 bool/str，`str(tuple)` 出来的
    `("a",)` **不是 YAML 语法**（YAML 的 flow sequence 用方括号），会被解析成字符串。"""
    if isinstance(value, tuple):
        return "[" + ", ".join(f'"{item}"' for item in value) + "]"
    return _yaml_value(value)


def test_fetch_values_are_actually_read(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    body = "fetch:\n" + "".join(
        f"  {key}: {_fetch_yaml_value(value)}\n" for key, value in FETCH_NON_DEFAULT.items()
    )
    config_path.write_text(body, encoding="utf-8")

    config = load_config(config_path, load_env=False)

    for key, expected in FETCH_NON_DEFAULT.items():
        actual = getattr(config.fetch_cfg, key)
        assert actual == expected, f"fetch.{key} 没被读到（YAML 写了 {expected}，实际 {actual}）"
        assert type(actual) is type(expected), f"fetch.{key} 的类型应保持 {type(expected)}"


def test_fetch_staged_timeouts_have_short_defaults(tmp_path: Path) -> None:
    """五个分阶段超时键不写也必须有默认值 —— 缺省即回退到"每段各给各的预算"。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("search:\n  providers: ['auto']\n", encoding="utf-8")
    fetch_cfg = load_config(config_path, load_env=False).fetch_cfg
    assert fetch_cfg.connect_timeout_s == DEFAULT_CONNECT_TIMEOUT_S
    assert fetch_cfg.robots_timeout_s == DEFAULT_ROBOTS_TIMEOUT_S
    assert fetch_cfg.extract_timeout_s == DEFAULT_EXTRACT_TIMEOUT_S
    assert fetch_cfg.mineru_poll_timeout_s == DEFAULT_MINERU_POLL_TIMEOUT_S
    # 不写 deadline_s → 出网层按 timeout_s × 1.5 算（这里只保证"不是写死的常数"）
    assert fetch_cfg.deadline_s is None

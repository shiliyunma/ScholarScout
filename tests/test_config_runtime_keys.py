"""配置读取的守卫：`RuntimeProfile` 的每个字段都必须真能从 YAML 读出来。

**为什么要有这条**：`load_config` 里是**手写字段列表**，加了新字段却忘了接一行，
YAML 里写什么都会被**静默忽略** —— 用户以为开了，实际没开。
这不是假设：`runtime.plan_by_model` 就这么白白失效过一次
（真机跑完两轮才发现"子问题 0 条"，因为读取处没这一行）。
所以这里用 `dataclasses.fields` **枚举**，以后加字段必须同步接进读取处。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from scholarscout.config import RuntimeProfile, load_config

#: 每个字段给一个**与默认值不同**的值，能区分"读到了"与"用了默认"
NON_DEFAULT_VALUES: dict[str, object] = {
    "max_tool_calls_per_turn": 7,
    "max_context_chars": 12345,
    "stream": True,
    "allow_clarify": False,
    "max_history_chars": 4321,
    "plan_by_model": True,
    # D3：加字段就必须同步这里，否则 YAML 里写这个键会被静默忽略（守卫测试会当场失败）
    "compact_after_chars": 5555,
    # 子 Agent P0：并行抓取的并发上限（fetch_many 读它）
    "max_parallel_fetches": 5,
    # 子 Agent P1：顺序子任务总开关（默认关；开着会多花模型调用）
    "subtask_enabled": True,
    # 飞书渠道回调监听（入口层，docs/17 §3）：开关 + 端口。默认关。
    "feishu_listener_enabled": True,
    "feishu_listener_port": 8766,
    # 监听路径（可配，便于与穿透工具对齐）
    "feishu_listener_path": "/custom/hook",
    # 飞书出站发送器开关（把回答推回飞书）。默认关。
    "feishu_reply_enabled": True,
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

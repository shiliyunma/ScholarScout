"""`ui:` 段的配置键必须有读取行。

为什么单列一条：`runtime.plan_by_model` 曾经"YAML 写了 true 却被静默忽略"——
因为 `load_config` 里的字段是手写列表，加了字段忘接读取行就什么都不报。
外观开关也一样：`ui.frameless_window: false` 读不出来 = 用户以为回退了、其实没有，
而且这个开关的用处正是"窗口拖不动时的保命绳"。所以每个字段都钉一条。
"""

from __future__ import annotations

from dataclasses import fields

from scholarscout.config import UiSettings, load_config


def test_every_ui_field_is_read_from_yaml(tmp_path) -> None:
    for field in fields(UiSettings):
        path = tmp_path / f"config-{field.name}.yaml"
        path.write_text(f"ui:\n  {field.name}: false\n", encoding="utf-8")
        loaded = load_config(path, load_env=False)
        assert getattr(loaded.ui, field.name) is False, f"{field.name} 没被读出来"


def test_frameless_defaults_to_off(tmp_path) -> None:
    """默认**关**：无边框在 Windows 后端拖动/缩放都不好使，宁多一行标题栏也不能让人动不了窗口。"""
    path = tmp_path / "config.yaml"
    path.write_text("runtime: {}\n", encoding="utf-8")
    assert UiSettings().frameless_window is False
    assert load_config(path, load_env=False).ui.frameless_window is False

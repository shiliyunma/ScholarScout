"""MinerU 的体积/页数上限：`config.example.yaml` 的 ``mineru:`` 四键必须**真被读到**。

为什么单独钉这一组：`mineru.agent_max_bytes` / `agent_max_pages` / `precise_max_bytes` /
`precise_max_pages` 这四个键在 YAML 里写了很久，但 `load_config` 不读、`mineru.py` 只把
10MB/20 页、200MB/200 页写在注释里 —— 于是"超限"只能等 MinerU 服务端回一句含糊的 failed，
用户分不清"文件太大"和"服务抽风"。这里钉三件事：

1. 配小了就**真按配的值**提前拒（本地判掉，一个请求都不发）；
2. 不配就是官方的 10MB/20 页与 200MB/200 页（与接配置之前的行为一致，不许悄悄变）；
3. 配成 ``0`` / 负数 / 字符串 / bool 时**加载配置就报错**，且报错里带键名
   （静默回默认 = 用户以为改了、其实没改，那正是这四个键烂掉的原因）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf
import pytest

from scholarscout.config import load_config
from scholarscout.fetch.mineru import parse_pdf_bytes
from scholarscout.fetch.settings import (
    DEFAULT_MINERU_AGENT_MAX_BYTES,
    DEFAULT_MINERU_AGENT_MAX_PAGES,
    DEFAULT_MINERU_PRECISE_MAX_BYTES,
    DEFAULT_MINERU_PRECISE_MAX_PAGES,
    MineruSettings,
)


class NoNetworkHttp:
    """**任何**出网调用都算失败：超限必须在本地就判掉，连一次签名请求都不该发。

    为什么不用"return 空响应"的假出网层：那样一旦没拦住，测试会拿到一个 failed 结果
    而**看着像通过**。这里直接炸，拦不住就红。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def post_json(self, url: str, payload: dict[str, Any], **kwargs: Any) -> object:
        self.calls.append(f"post_json {url}")
        raise AssertionError(f"超限了却还发了请求：{url}")

    def upload_bytes(self, url: str, body: bytes, **kwargs: Any) -> object:
        self.calls.append(f"upload_bytes {url}")
        raise AssertionError(f"超限了却还上传了字节：{url}")

    def fetch(self, url: str, **kwargs: Any) -> object:
        self.calls.append(f"fetch {url}")
        raise AssertionError(f"超限了却还去轮询：{url}")


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _pdf_with_pages(count: int) -> bytes:
    """造一份**真 PDF**（页数上限只能靠真数页数来验）。"""
    document = pymupdf.open()
    for _ in range(count):
        document.new_page()
    data = document.tobytes()
    document.close()
    return data


def test_agent_page_limit_from_config_refuses_without_network(tmp_path: Path) -> None:
    """``mineru.agent_max_pages: 3`` → 4 页的 PDF 在上传前就被拒（不真调接口）。"""
    config = load_config(_write_config(tmp_path, "mineru:\n  agent_max_pages: 3\n"), load_env=False)
    assert config.mineru.agent_max_pages == 3  # 键真的被读了，不是"写了没人看"

    http = NoNetworkHttp()
    result = parse_pdf_bytes(http, _pdf_with_pages(4), limits=config.mineru)

    assert result.state == "failed"
    assert "mineru.agent_max_pages" in result.detail
    assert "3" in result.detail
    assert http.calls == []


def test_agent_byte_limit_from_config_refuses_without_network(tmp_path: Path) -> None:
    """``mineru.agent_max_bytes`` 配小 → 按配的字节数拒（非 PDF 也一样拦）。"""
    config = load_config(
        _write_config(tmp_path, "mineru:\n  agent_max_bytes: 1000\n"), load_env=False
    )
    assert config.mineru.agent_max_bytes == 1000

    http = NoNetworkHttp()
    result = parse_pdf_bytes(http, b"x" * 1001, limits=config.mineru)

    assert result.state == "failed"
    assert "mineru.agent_max_bytes" in result.detail
    assert http.calls == []


def test_precise_byte_limit_from_config_refuses_instead_of_falling_back(tmp_path: Path) -> None:
    """精准接口超限**不许**退回 Agent 硬塞一遍（那等于绕开用户配的上限）。"""
    config = load_config(
        _write_config(tmp_path, "mineru:\n  precise_max_bytes: 50\n"), load_env=False
    )
    assert config.mineru.precise_max_bytes == 50

    http = NoNetworkHttp()
    result = parse_pdf_bytes(http, b"x" * 100, token="tk", limits=config.mineru)

    assert result.state == "failed"
    assert result.engine == "precise"
    assert "mineru.precise_max_bytes" in result.detail
    assert http.calls == []


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("agent_max_pages: 0", "mineru.agent_max_pages"),
        ("agent_max_pages: -5", "mineru.agent_max_pages"),
        ("agent_max_bytes: 0", "mineru.agent_max_bytes"),
        ("precise_max_pages: '20'", "mineru.precise_max_pages"),
        ("precise_max_pages: true", "mineru.precise_max_pages"),
        ("precise_max_bytes: 1.5", "mineru.precise_max_bytes"),
    ],
)
def test_invalid_limit_fails_at_config_load_with_key_name(
    tmp_path: Path, section: str, key: str
) -> None:
    """``<=0`` / 字符串 / bool / 小数 → **加载配置时**就报错，且报错里带键名。

    为什么必须带键名：这四个键长得像、又都以 ``max_`` 开头，只说"必须是正整数"用户改不动。
    为什么不在调用处默默回默认：那就是"配了等于没配"，也正是这四个键烂了这么久没人发现的原因。
    """
    with pytest.raises(ValueError) as excinfo:
        load_config(_write_config(tmp_path, f"mineru:\n  {section}\n"), load_env=False)
    assert key in str(excinfo.value)


def test_unconfigured_limits_fall_back_to_official_10mb_20p_and_200mb_200p(
    tmp_path: Path,
) -> None:
    """没配 ``mineru:`` 段 → 回改造前写死的 10MB/20 页与 200MB/200 页（行为不变）。"""
    config = load_config(
        _write_config(tmp_path, "features:\n  enable_mineru: true\n"), load_env=False
    )

    assert config.mineru.agent_max_bytes == 10_000_000 == DEFAULT_MINERU_AGENT_MAX_BYTES
    assert config.mineru.agent_max_pages == 20 == DEFAULT_MINERU_AGENT_MAX_PAGES
    assert config.mineru.precise_max_bytes == 200_000_000 == DEFAULT_MINERU_PRECISE_MAX_BYTES
    assert config.mineru.precise_max_pages == 200 == DEFAULT_MINERU_PRECISE_MAX_PAGES

    # 不只是"默认值好看"：默认值**真的在拦**（10MB 这条以前只写在注释里）
    http = NoNetworkHttp()
    result = parse_pdf_bytes(http, b"x" * (DEFAULT_MINERU_AGENT_MAX_BYTES + 1), limits=None)
    assert result.state == "failed"
    assert "mineru.agent_max_bytes" in result.detail
    assert http.calls == []
    assert MineruSettings() == MineruSettings.from_raw(None)

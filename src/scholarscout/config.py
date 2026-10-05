"""配置加载。

- **设置**来自 `config.yaml`；**密钥**只从 `.env` 或环境变量读（照 Hermes 的做法：设置与密钥分离）
- 配置文件不存在时用内置默认值（保证测试与首次运行不需要先造配置）
- 环境变量 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` 可覆盖（方便临时指向本地 Ollama）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from scholarscout import paths
from scholarscout.fetch.settings import FetchSettings
from scholarscout.providers.settings import SearchSettings
from scholarscout.summary.settings import SummarySettings

DEFAULT_CHAT = {
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com/v1",
    "temperature": 0.2,
    #: 推理模型（如 deepseek-flash）的 reasoning token 也算 completion，额度必须留足
    "max_tokens": 16384,
    "timeout_s": 60,
}
DEFAULT_SUMMARY = {
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com/v1",
    "temperature": 0.1,
    "max_tokens": 4096,
    "timeout_s": 60,
}


@dataclass(frozen=True)
class LlmProfile:
    """一个模型档位（对话档 / 摘要档）。"""

    model: str
    base_url: str
    temperature: float = 0.2
    max_tokens: int = 16384
    timeout_s: float = 60.0
    api_key_env: str = "LLM_API_KEY"

    @property
    def chat_completions_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def api_key(self) -> str | None:
        value = os.environ.get(self.api_key_env, "").strip()
        return value or None


@dataclass(frozen=True)
class RuntimeProfile:
    max_tool_calls_per_turn: int = 12
    max_context_chars: int = 60000
    stream: bool = False
    allow_clarify: bool = True


@dataclass(frozen=True)
class Features:
    enable_kb: bool = False
    enable_mineru: bool = True
    enable_topic_map: bool = True
    enable_route_plan: bool = True


@dataclass(frozen=True)
class Config:
    chat: LlmProfile
    summary: LlmProfile
    runtime: RuntimeProfile
    features: Features
    data_dir: Path
    db_path: Path
    raw: dict[str, Any] = field(default_factory=dict)
    summary_cfg: SummarySettings = field(
        default_factory=lambda: SummarySettings.from_raw(None, home=paths.home())
    )
    fetch_cfg: FetchSettings = field(default_factory=FetchSettings)
    search_cfg: SearchSettings = field(default_factory=SearchSettings)

    def api_key(self, profile: LlmProfile | None = None) -> str | None:
        return (profile or self.chat).api_key()


def _profile(section: dict[str, Any], defaults: dict[str, Any]) -> LlmProfile:
    merged = {**defaults, **{k: v for k, v in section.items() if v is not None}}
    env_model = os.environ.get("LLM_MODEL", "").strip()
    env_base = os.environ.get("LLM_BASE_URL", "").strip()
    if env_model:
        merged["model"] = env_model
    if env_base:
        merged["base_url"] = env_base
    return LlmProfile(
        model=str(merged["model"]),
        base_url=str(merged["base_url"]),
        temperature=float(merged["temperature"]),
        max_tokens=int(merged["max_tokens"]),
        timeout_s=float(merged["timeout_s"]),
    )


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """极简 .env 解析（不引第三方库）。已存在的环境变量优先，不被覆盖。"""
    target = path or (paths.home() / ".env")
    loaded: dict[str, str] = {}
    if not target.is_file():
        return loaded
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def load_config(config_path: Path | None = None, *, load_env: bool = True) -> Config:
    """读配置。文件不存在则全用默认值。"""
    if load_env:
        load_dotenv()
    target = config_path or (paths.home() / "config.yaml")
    raw: dict[str, Any] = {}
    if target.is_file():
        parsed = yaml.safe_load(target.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            raw = parsed

    llm_section = raw.get("llm", {}) if isinstance(raw.get("llm"), dict) else {}
    runtime_section = raw.get("runtime", {}) if isinstance(raw.get("runtime"), dict) else {}
    feature_section = raw.get("features", {}) if isinstance(raw.get("features"), dict) else {}

    search_cfg = SearchSettings.from_raw(raw.get("search"))
    fetch_cfg = FetchSettings.from_raw(raw.get("fetch"))
    if search_cfg.robots_exempt_hosts():
        # 只有显式打开"抓搜索结果页"开关时，才把搜索页主机加进 robots 豁免名单；
        # 默认不动 robots（Bing 的 robots.txt 明确 Disable /search，见 providers/settings.py）
        fetch_cfg = replace(
            fetch_cfg,
            robots_exempt_hosts=(*fetch_cfg.robots_exempt_hosts, *search_cfg.robots_exempt_hosts()),
        )

    return Config(
        chat=_profile(llm_section.get("chat", {}) or {}, DEFAULT_CHAT),
        summary=_profile(llm_section.get("summary", {}) or {}, DEFAULT_SUMMARY),
        runtime=RuntimeProfile(
            max_tool_calls_per_turn=int(runtime_section.get("max_tool_calls_per_turn", 12)),
            max_context_chars=int(runtime_section.get("max_context_chars", 60000)),
            stream=bool(runtime_section.get("stream", False)),
            allow_clarify=bool(runtime_section.get("allow_clarify", True)),
        ),
        features=Features(
            enable_kb=bool(feature_section.get("enable_kb", False)),
            enable_mineru=bool(feature_section.get("enable_mineru", True)),
            enable_topic_map=bool(feature_section.get("enable_topic_map", True)),
            enable_route_plan=bool(feature_section.get("enable_route_plan", True)),
        ),
        data_dir=paths.data_dir(),
        db_path=paths.db_path(),
        raw=raw,
        summary_cfg=SummarySettings.from_raw(raw.get("summary"), home=paths.home()),
        fetch_cfg=fetch_cfg,
        search_cfg=search_cfg,
    )

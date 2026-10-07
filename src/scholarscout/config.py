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
    #: 是否让推理模型"深度思考"。
    #: 关掉时请求体带 `thinking: {"type": "disabled"}` —— 探针实测 completion 162 → 36（−78%）。
    #:
    #: **A/B 实测（2026-10-06，同一组 3 道探针题、同提示词，唯一变量就是这个开关）**：
    #: token 合计 −26%（118,816 → 87,562），但质量明显下降 ——
    #: 降级率 0% → 33.3%、产物完整率 100% → 66.7%、结尾主动询问 100% → 33.3%、
    #: 一次任务把工具预算打满（`tool_limit`）。
    #: **结论：不建议日常关**。只有"这次只想要个大概、能省就省"时才关。
    thinking: bool = True
    #: 是否把模型回复按请求体哈希缓存到本地。**默认关** —— 交互几乎不可能命中
    #: （每轮内容都是新的），它只对"同一请求原样重放"（评测复跑、调试）有价值。
    llm_cache: bool = False

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
    #: 历史正文的字符预算（保头保尾裁剪，防长会话 token 线性膨胀）
    max_history_chars: int = 12000
    #: 开局是否用一次小调用把目标拆成子问题（A1）。**默认关**：它会多一次模型调用，
    #: 打开就改变了原有的回复序列（测试里的脚本化假模型会被它吃掉一次），所以由 config.yaml 显式打开。
    #: 代价是每**会话**一次小调用（不是每轮）；关掉就退化成"只有目标 + 只有缺口"，粒度为粗。
    plan_by_model: bool = False


@dataclass(frozen=True)
class Features:
    enable_kb: bool = False
    enable_mineru: bool = True
    enable_topic_map: bool = True
    enable_route_plan: bool = True


@dataclass(frozen=True)
class Generation:
    """生成策略。

    `auto_artifacts=False`（默认）：**先问用户再生成产物** —— 2026-10-05 用户明确要求
    "知识地图/学习路线/抽背卡应该给用户选择的空间，不应该第一次会话就生成"。除了省 token，
    更重要的是别把用户没要的东西塞给他。
    `True` 是给论文留的对照臂（旧行为：每轮自动产出"主题图 + 路线"）。
    """

    auto_artifacts: bool = False
    #: 用模型把用户第一句总结成会话标题（**轮次开始前**做，标题立刻出现）。
    #: 关掉就用首句本地截断；开着每次新建会话多一次极小调用（几十 token）。
    title_by_model: bool = True
    #: D2 跨会话记忆的自动抽取：每**会话**最多一次小调用，且要有"值得记"的迹象。
    #: 关掉之后仍可在「研究看板 → 跨会话记忆」手动点「从现在整理」。
    memory_by_model: bool = True


@dataclass(frozen=True)
class Config:
    chat: LlmProfile
    summary: LlmProfile
    runtime: RuntimeProfile
    features: Features
    data_dir: Path
    db_path: Path
    generation: Generation = field(default_factory=Generation)
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
        thinking=bool(merged.get("thinking", True)),
        llm_cache=bool(merged.get("llm_cache", False)),
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
    generation_section = (
        raw.get("generation", {}) if isinstance(raw.get("generation"), dict) else {}
    )

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
            max_history_chars=int(runtime_section.get("max_history_chars", 12000)),
            plan_by_model=bool(runtime_section.get("plan_by_model", False)),
        ),
        features=Features(
            enable_kb=bool(feature_section.get("enable_kb", False)),
            enable_mineru=bool(feature_section.get("enable_mineru", True)),
            enable_topic_map=bool(feature_section.get("enable_topic_map", True)),
            enable_route_plan=bool(feature_section.get("enable_route_plan", True)),
        ),
        data_dir=paths.data_dir(),
        db_path=paths.db_path(),
        generation=Generation(
            auto_artifacts=bool(generation_section.get("auto_artifacts", False)),
            title_by_model=bool(generation_section.get("title_by_model", True)),
            # D2：跨会话记忆的自动抽取（每会话最多一次小调用）。关掉仍可手动整理。
            memory_by_model=bool(generation_section.get("memory_by_model", True)),
        ),
        raw=raw,
        summary_cfg=SummarySettings.from_raw(raw.get("summary"), home=paths.home()),
        fetch_cfg=fetch_cfg,
        search_cfg=search_cfg,
    )

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


#: 允许的风格档（D4）。提示词文本在 `runtime/prompts.py::STYLE_PROMPTS` ——
#: 文本属于 runtime 层，config 只做"取值合不合法"的校验（config 不能反向依赖 runtime）。
#: 两处必须一致，由 `tests/runtime/test_style_prompt.py` 钉住，免得加了档位忘了接。
STYLE_CHOICES: tuple[str, ...] = ("default", "academic", "tutor", "concise")


def normalize_style(value: Any) -> str:
    """校验风格档。**不认识的值直接报错** —— 静默回默认档等于"用户以为改了、其实没改"。"""
    style = str(value or "default").strip().lower()
    if style not in STYLE_CHOICES:
        raise ValueError(f"未知的风格档：{value!r}（允许：{'、'.join(STYLE_CHOICES)}）")
    return style


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
    #: D3：未被提要覆盖的那段超过这么多字符，才值得为它花一次模型调用去做摘要式压缩。
    #: 太短的话规则式裁剪就够了（压缩是"每压一段一次调用"，不是免费的）。
    compact_after_chars: int = 4000
    #: 开局是否用一次小调用把目标拆成子问题（A1）。**默认关**：它会多一次模型调用，
    #: 打开就改变了原有的回复序列（测试里的脚本化假模型会被它吃掉一次），所以由 config.yaml 显式打开。
    #: 代价是每**会话**一次小调用（不是每轮）；关掉就退化成"只有目标 + 只有缺口"，粒度为粗。
    plan_by_model: bool = False
    #: 子 Agent P0（docs/16 §3.3）：`research.search` 里并行抓取来源的并发上限。
    #: 只并行**出网**（provider.search），绝不并发调模型；实际限速仍由 `fetch/http.py`
    #: 按 host 串行化，所以这里调大不会绕过任何来源的 `min_interval_s`。
    max_parallel_fetches: int = 3
    #: 子 Agent P1（docs/16 §3.3）：**顺序子任务**（ReWOO 式"先规划后执行"）总开关。
    #: **默认关** —— 每个子任务是一次隔离的 mini-loop（独立 messages + 小预算），只把最终文本
    #: 并回主 loop；开着一定会多花模型调用。整批成本由**总额度池**兜住
    #: （默认且上限 = `max_tool_calls_per_turn` 的 1/2），记账 `usage_records.purpose="subtask"`。
    #: 放 RuntimeProfile（同 feishu 开关）是为了让 `tests/test_config_runtime_keys.py` 的守卫覆盖到它。
    subtask_enabled: bool = False
    #: 飞书渠道回调监听开关（入口层，docs/17 §3）。**默认关**：开启后才用标准库
    #: `http.server` 监听本地端口等飞书 POST 事件（只绑 127.0.0.1，公网交给穿透/反代）。
    #: 放在 RuntimeProfile 里（而不是新开一个档）是为了让 `tests/test_config_runtime_keys.py`
    #: 的守卫覆盖到它 —— 否则 YAML 里写这个键会被 `load_config` 静默忽略（D3 踩过同款坑）。
    feishu_listener_enabled: bool = False
    #: 回调监听端口（照 Hermes 适配器默认 8765；enabled=false 时不起服务，端口无意义）。
    feishu_listener_port: int = 8765
    #: 回调路径（照 Hermes 默认 `/feishu/webhook`，adapter.py:151）；可配，便于与穿透工具对齐。
    feishu_listener_path: str = "/feishu/webhook"
    #: 飞书**出站发送器**开关（把一轮回答推回飞书，docs/17 §3.7）。**默认关**：
    #: 关着时渠道层只记录、绝不发任何外网（与监听开关分开：监听=收，发送=发）。
    feishu_reply_enabled: bool = False


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
    #: 记忆抽取的**按轮触发**（nudge，照 Hermes 的 `nudge_interval`，docs/16 §2 P1-1）。
    #: `0`（**默认**）＝关：仍是"每会话最多一次"，成本口径与引入前完全一致；
    #: `>0`＝距上次抽取的用户轮数 ≥ 该值就再抽一次（续写式，复用上次素材）。
    #: 开启后约每 n 轮多一次小调用（`purpose="memory"` 已记账，可在 `session.usage` 看到）。
    memory_nudge_turns: int = 0
    #: D4 人格/风格档：default | academic | tutor | concise。
    #: **默认档不加任何字符**，所以不改它就不影响实验臂哈希（E3 那批数字仍可比）。
    style: str = "default"
    #: D3 历史摘要式压缩：历史超阈值时用一次模型调用把中段压成结构化提要（每段只压一次）。
    #: 关掉则退回规则式裁剪（省调用，但被裁掉的中段就真丢了）。
    compact_by_model: bool = True


@dataclass(frozen=True)
class UiSettings:
    """桌面外壳的外观。**只影响窗口本身，不碰任何数据。**

    `frameless_window`：去掉原生标题行，改成自绘窗控条 + 右下角抓手。

    **默认关，这是踩过之后的决定**：pywebview 在 Windows 上的无边框窗口既不响应标题区拖动，
    也没有系统边框可拖（实测：拖动位移 0、右缘缩放无效）。后来自己用 JS 打桥实现移动/缩放，
    在本机 WebView2 里也没能跑通。所以对外默认用普通窗口 —— 能移动、能缩放、能边缘拉伸，
    代价是多一行标题栏。要试无边框就把这里改成 true（自绘窗控条与抓手的代码都还在）。
    """

    frameless_window: bool = False

    @classmethod
    def from_raw(cls, section: object) -> UiSettings:
        data = section if isinstance(section, dict) else {}
        return cls(frameless_window=bool(data.get("frameless_window", False)))


@dataclass(frozen=True)
class LibraryProfile:
    """资料落地布局（docs/19 §2）：**目录如何切分**，从外到内。

    默认 ``("session", "format")`` = ``data/raw/<会话>/<格式>/`` —— 按会话分化、会话内按格式分类。
    可用段（``paths.LAYOUT_SEGMENTS``）：``session``（会话，同名时目录名带短会话 id）/
    ``format``（格式，取自 ``sources.source_type``；``type`` 是它的旧称）/
    ``collected``（收录年月 = ``documents.fetched_at``）/ ``published``（发表年月）。

    为什么做成配置：这是用户会反复改的**显示约定**，而且原来被写死在三处（``paths`` / ``tools`` /
    迁移脚本），改一次要三处同时改对、还容易漏。现在三处都读这一个值（``docs/19 §2``）。
    未知段**直接报错**（静默回默认 = 用户以为改了、其实还在老位置写文件）。
    """

    layout: tuple[str, ...] = paths.DEFAULT_LAYOUT

    @classmethod
    def from_raw(cls, section: object) -> LibraryProfile:
        data = section if isinstance(section, dict) else {}
        return cls(layout=paths.normalize_layout(data.get("layout")))


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
    ui: UiSettings = field(default_factory=UiSettings)
    library: LibraryProfile = field(default_factory=LibraryProfile)

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

    ui_cfg = UiSettings.from_raw(raw.get("ui"))
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
            compact_after_chars=int(runtime_section.get("compact_after_chars", 4000)),
            plan_by_model=bool(runtime_section.get("plan_by_model", False)),
            # 子 Agent P0：并行抓取的并发上限（D3 同款坑：加了字段必须接这一行，否则静默失效）
            max_parallel_fetches=int(runtime_section.get("max_parallel_fetches", 3)),
            # 子 Agent P1：顺序子任务总开关（默认关）。D3 同款坑：加了字段必须接这一行。
            subtask_enabled=bool(runtime_section.get("subtask_enabled", False)),
            # 飞书渠道回调监听（入口层，docs/17 §3）。默认关；同款坑：必须接这一行。
            feishu_listener_enabled=bool(runtime_section.get("feishu_listener_enabled", False)),
            feishu_listener_port=int(runtime_section.get("feishu_listener_port", 8765)),
            feishu_listener_path=str(
                runtime_section.get("feishu_listener_path", "/feishu/webhook")
            ),
            # 飞书出站发送（把回答推回飞书）。默认关 → 关着时渠道层绝不发任何外网。
            feishu_reply_enabled=bool(runtime_section.get("feishu_reply_enabled", False)),
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
            # P1-1：记忆按轮 nudge（0=默认关，成本口径不变）。必须接这一行否则静默失效。
            memory_nudge_turns=int(generation_section.get("memory_nudge_turns", 0)),
            # 认识的风格档才认；不认识就**报错**（静默回默认＝用户以为改了其实没改）
            style=normalize_style(generation_section.get("style", "default")),
            compact_by_model=bool(generation_section.get("compact_by_model", True)),
        ),
        raw=raw,
        summary_cfg=SummarySettings.from_raw(raw.get("summary"), home=paths.home()),
        fetch_cfg=fetch_cfg,
        search_cfg=search_cfg,
        ui=ui_cfg,
        library=LibraryProfile.from_raw(raw.get("library")),
    )

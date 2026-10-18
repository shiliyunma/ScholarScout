"""设置的读写：**校验 → 深合并 → 原子写回**，密钥只写不回读。

照 Hermes 的分工：**设置进 `config.yaml`、密钥进 `.env`**（本模块两者都管，但读回来的只有
"是否已配置"这种布尔，绝不把密钥送回前端）。

三条纪律：
1. **只认白名单里的字段**：写未知字段直接报错 —— 否则一个拼错的键会静默不生效，
   用户以为改了、实际什么都没变（比报错难受得多）；
2. **保留原文件里的其它键**：在原始 YAML 上深合并再整体写回，
   所以用户自己加的 `openalex_mailto` / `searxng_base_url` / `min_interval_s` 不会被抹掉；
3. **原子写**：先写 `.tmp` 再 `os.replace` —— 崩在中间也不能把配置截断成半截文件。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from scholarscout import paths
from scholarscout.config import STYLE_CHOICES, Config, load_config
from scholarscout.providers.thinking import LEVELS


class SettingsError(ValueError):
    """设置值不合法（RPC 层会翻成 -32602 参数错误）。"""


@dataclass(frozen=True)
class Field:
    """一个可编辑字段：路径 + 类型 + 取值范围。"""

    path: tuple[str, ...]
    kind: str  # "str" | "int" | "float" | "bool"
    minimum: float | None = None
    maximum: float | None = None
    must_be_url: bool = False
    label: str = ""
    #: 配置里还没写这个键时界面该显示什么。**必填**语义要小心：
    #: 布尔默认成 True 会让"默认关"的开关看起来是开的（用户以为改了实际没改）。
    default: Any = None
    #: 取值白名单（如风格档）。**有它就只收白名单里的值** ——
    #: 面板上写错一个值，会在下次启动 load_config 时才炸；在这里拦住才是对的时机。
    choices: tuple[str, ...] | None = None

    def check(self, value: Any) -> Any:
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise SettingsError(f"{self.label} 需要 true/false")
            return value
        if self.kind in {"int", "float"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SettingsError(f"{self.label} 需要数字")
            number = int(value) if self.kind == "int" else float(value)
            if self.minimum is not None and number < self.minimum:
                raise SettingsError(f"{self.label} 不能小于 {self.minimum}")
            if self.maximum is not None and number > self.maximum:
                raise SettingsError(f"{self.label} 不能大于 {self.maximum}")
            return number
        text = str(value).strip()
        if not text:
            raise SettingsError(f"{self.label} 不能为空")
        if self.choices is not None and text not in self.choices:
            raise SettingsError(f"{self.label} 只能是：{'、'.join(self.choices)}")
        if self.must_be_url and not (text.startswith("http://") or text.startswith("https://")):
            raise SettingsError(f"{self.label} 必须是 http(s):// 开头的地址")
        return text


def _f(*path: str, kind: str, label: str, **kwargs: Any) -> tuple[str, Field]:
    """一条字段定义：键是点分路径（``llm.chat.model``），Field 里留元组便于深合并。"""
    return ".".join(path), Field(path=path, kind=kind, label=label, **kwargs)


#: 白名单：能改的字段（键是 ``llm.chat.model`` 这种点分路径）
FIELDS: dict[str, Field] = dict(
    [
        _f("llm", "chat", "model", kind="str", label="对话模型名"),
        _f("llm", "chat", "base_url", kind="str", label="对话模型地址", must_be_url=True),
        _f(
            "llm",
            "chat",
            "temperature",
            kind="float",
            label="对话 temperature",
            minimum=0.0,
            maximum=2.0,
        ),
        _f(
            "llm",
            "chat",
            "max_tokens",
            kind="int",
            label="对话 max_tokens",
            minimum=256,
            maximum=65536,
        ),
        _f(
            "llm", "chat", "timeout_s", kind="float", label="对话超时（秒）", minimum=5, maximum=600
        ),
        _f(
            "llm",
            "chat",
            "thinking",
            kind="bool",
            label="深度思考（关掉省 ~26% token，但实测质量下降，不建议日常关）",
            default=True,
        ),
        _f(
            "llm",
            "chat",
            "reasoning_effort",
            kind="str",
            label="思考深度（最小/低/中/高/最高/关；本服务真实档位只有 low/high/max）",
            default="medium",
            choices=LEVELS,
        ),
        _f(
            "llm",
            "chat",
            "llm_cache",
            kind="bool",
            label="回复缓存（评测复跑用，交互命中率≈0）",
            default=False,
        ),
        _f("llm", "summary", "model", kind="str", label="摘要模型名"),
        _f("llm", "summary", "base_url", kind="str", label="摘要模型地址", must_be_url=True),
        _f(
            "llm",
            "summary",
            "max_tokens",
            kind="int",
            label="摘要 max_tokens",
            minimum=256,
            maximum=65536,
        ),
        _f(
            "llm",
            "summary",
            "thinking",
            kind="bool",
            label="摘要档深度思考",
            default=True,
        ),
        _f(
            "runtime",
            "max_tool_calls_per_turn",
            kind="int",
            label="单轮工具预算",
            minimum=1,
            maximum=50,
        ),
        _f(
            "runtime",
            "max_context_chars",
            kind="int",
            label="单文档正文上限",
            minimum=5000,
            maximum=500000,
        ),
        _f(
            "runtime",
            "max_history_chars",
            kind="int",
            label="历史正文预算（字符）",
            minimum=1000,
            maximum=200000,
        ),
        _f("runtime", "allow_clarify", kind="bool", label="允许反问澄清"),
        _f(
            "runtime",
            "compact_after_chars",
            kind="int",
            label="D3 压缩阈值（字符）",
            minimum=1200,
            maximum=200000,
        ),
        _f(
            "generation",
            "compact_by_model",
            kind="bool",
            label="历史用模型摘要式压缩（关＝只做规则裁剪）",
            default=True,
        ),
        _f("features", "enable_mineru", kind="bool", label="扫描版走 MinerU"),
        _f("features", "enable_topic_map", kind="bool", label="允许生成主题地图"),
        _f("features", "enable_route_plan", kind="bool", label="允许生成学习路线"),
        # 默认关：产物由用户主动选择才生成（2026-10-05 用户明确要求），别默认成开
        _f(
            "generation",
            "auto_artifacts",
            kind="bool",
            label="产物自动生成（关＝先询问）",
            default=False,
        ),
        _f(
            "generation",
            "title_by_model",
            kind="bool",
            label="标题用模型总结（关＝用首句截断）",
            default=True,
        ),
        _f(
            "generation",
            "style",
            kind="str",
            label="风格档（default／academic 严谨学术／tutor 亲切助教／concise 简洁）",
            default="default",
            choices=STYLE_CHOICES,
        ),
        _f("search", "searxng_base_url", kind="str", label="自建 SearxNG 地址"),
        _f("search", "google_cse_cx", kind="str", label="Google CSE 搜索引擎 ID（CX）"),
        _f(
            "search",
            "allow_searchpage_scraping",
            kind="bool",
            label="允许抓搜索结果页（默认关：Bing robots 禁 /search）",
            default=False,
        ),
        _f("search", "openalex_mailto", kind="str", label="OpenAlex 联系邮箱"),
    ]
)

#: 写进 .env 的密钥字段（**只写不回读**）
SECRET_FIELDS = {
    "llm.api_key": "LLM_API_KEY",
    "search.tavily_api_key": "TAVILY_API_KEY",
    "search.serper_api_key": "SERPER_API_KEY",
    "search.brave_api_key": "BRAVE_API_KEY",
    "search.google_cse_key": "GOOGLE_CSE_KEY",
    "search.s2_api_key": "S2_API_KEY",
    "search.github_token": "GITHUB_TOKEN",
    "mineru.api_token": "MINERU_API_TOKEN",
}

#: `.env` 里会**盖掉** config.yaml 的键（照 config.py 的 `_profile`：方便临时指向本地 Ollama）。
#: 用户在设置栏改这些字段时，必须把覆盖摘掉，否则"改了没生效"是静默的 —— 最难查的一类。
ENV_OVERRIDES = {
    "llm.chat.model": "LLM_MODEL",
    "llm.chat.base_url": "LLM_BASE_URL",
}

DEFAULT_CONFIG_NAME = "config.yaml"
DEFAULT_ENV_NAME = ".env"


def _deep_set(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = target
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value


def _deep_get(source: dict[str, Any], path: tuple[str, ...], fallback: Any = None) -> Any:
    node: Any = source
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return fallback
        node = node[key]
    return node


def _flatten(updates: dict[str, Any], prefix: tuple[str, ...] = ()) -> dict[str, Any]:
    """把嵌套的更新字典摊成 ``{"llm.chat.model": "x"}``。"""
    flat: dict[str, Any] = {}
    for key, value in updates.items():
        if isinstance(value, dict):
            flat.update(_flatten(value, (*prefix, str(key))))
        else:
            flat[".".join((*prefix, str(key)))] = value
    return flat


def remember_models(
    config: Config, models: list[str] | tuple[str, ...], *, config_path: Path | None = None
) -> Config:
    """把探测到的候选模型写进 ``config.yaml`` 的 ``llm.chat.available_models``。

    为什么单独一个入口（而不塞进 FIELDS）：它是**列表**，不是用户在面板里手填的标量；
    但落盘同样要守住"深合并 + 原子写"，否则会抹掉用户自己加的键。
    只写名单本身：**不动当前模型**（选择是用户的事，探测只负责告诉有哪些可选）。
    """
    names: list[str] = []
    for item in models:
        name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    target = config_path or config_path_of(config)
    raw = dict(config.raw) if isinstance(config.raw, dict) else {}
    merged = _deep_copy(raw)
    _deep_set(merged, ("llm", "chat", "available_models"), names)
    _atomic_write_yaml(target, merged)
    return load_config(target)


def read_settings(config: Config) -> dict[str, Any]:
    """给界面用的当前设置（**不含任何密钥值**，只报"是否已配置"）。"""
    raw = config.raw if isinstance(config.raw, dict) else {}
    values: dict[str, Any] = {}
    for path, field in FIELDS.items():
        parts = tuple(path.split("."))
        value = _deep_get(raw, parts)
        if value is None:
            value = field.default
        if value is None:
            value = _deep_get(
                {
                    "llm": {
                        "chat": {
                            "model": config.chat.model,
                            "base_url": config.chat.base_url,
                            "temperature": config.chat.temperature,
                            "max_tokens": config.chat.max_tokens,
                            "timeout_s": config.chat.timeout_s,
                            "reasoning_effort": config.chat.reasoning_effort,
                        },
                        "summary": {
                            "model": config.summary.model,
                            "base_url": config.summary.base_url,
                            "max_tokens": config.summary.max_tokens,
                        },
                    },
                    "runtime": {
                        "max_tool_calls_per_turn": config.runtime.max_tool_calls_per_turn,
                        "max_context_chars": config.runtime.max_context_chars,
                        "allow_clarify": config.runtime.allow_clarify,
                    },
                    "features": {
                        "enable_mineru": config.features.enable_mineru,
                        "enable_topic_map": config.features.enable_topic_map,
                        "enable_route_plan": config.features.enable_route_plan,
                    },
                },
                parts,
            )
        values[path] = value
    secrets = {
        name: bool(os.environ.get(env_name, "").strip()) for name, env_name in SECRET_FIELDS.items()
    }
    return {
        "values": values,
        "secrets_configured": secrets,
        "config_path": str(config_path_of(config)),
    }


def config_path_of(config: Config) -> Path:
    """config.yaml 的位置：环境变量优先，否则项目根下（与 load_config 的规则一致）。"""
    override = os.environ.get("SCHOLARSCOUT_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return paths.home() / DEFAULT_CONFIG_NAME


def apply_updates(
    config: Config,
    updates: dict[str, Any],
    *,
    config_path: Path | None = None,
    env_path: Path | None = None,
    notes: list[str] | None = None,
) -> Config:
    """校验并写回，返回**重新加载**后的 Config（调用方据此重建模型客户端）。

    ``notes`` 会累积"顺手做的事"（例如摘掉 `.env` 里会覆盖配置的环境变量），
    RPC 层把它们回给界面 —— 用户应该知道为什么这次改动真的生效了。
    """
    flat = _flatten(updates)
    if not flat:
        raise SettingsError("没有要修改的设置")

    checked: dict[str, Any] = {}
    secrets: dict[str, str] = {}
    for key, value in flat.items():
        if key in SECRET_FIELDS:
            text = str(value or "").strip()
            if not text:
                raise SettingsError(f"{key} 不能为空（想清除请直接编辑 .env）")
            secrets[SECRET_FIELDS[key]] = text
            continue
        field = FIELDS.get(key)
        if field is None:
            raise SettingsError(f"不认识的设置项：{key}")
        checked[key] = field.check(value)

    target = config_path or config_path_of(config)
    raw = dict(config.raw) if isinstance(config.raw, dict) else {}
    merged = _deep_copy(raw)
    for key, value in checked.items():
        _deep_set(merged, tuple(key.split(".")), value)

    if checked:
        _atomic_write_yaml(target, merged)

    dotenv = env_path or (paths.home() / DEFAULT_ENV_NAME)
    # 改了会被 .env 覆盖的字段 → 把那条覆盖摘掉，不然用户会以为设置没生效
    for key, env_name in ENV_OVERRIDES.items():
        if key not in checked:
            continue
        if os.environ.get(env_name, "").strip():
            os.environ.pop(env_name, None)
        if drop_env_key(dotenv, env_name) and notes is not None:
            notes.append(f"已移除 .env 里的 {env_name}（它原本会覆盖你刚改的值）")

    if secrets:
        write_env(dotenv, secrets)
        for env_name, text in secrets.items():
            os.environ[env_name] = text  # 立即生效，不必重启

    return load_config(target)


def _deep_copy(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: _deep_copy(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_deep_copy(item) for item in node]
    return node


def _atomic_write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_env(path: Path, values: dict[str, str]) -> None:
    """把密钥写进 ``.env``：已有的键就地替换，没有的追加，其它行原样保留。"""
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(values)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    for key, value in remaining.items():
        lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def drop_env_key(path: Path, key: str) -> bool:
    """删掉 ``.env`` 里的某个键（保留其它行与注释）。删到了返回 True。"""
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    removed = False
    for line in lines:
        stripped = line.strip()
        key_of = ""
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key_of = stripped.split("=", 1)[0].strip()
        if key_of and key_of == key:
            removed = True
            continue
        kept.append(line)
    if removed:
        path.write_text("\n".join(kept).rstrip("\n") + "\n", encoding="utf-8")
    return removed


def _dir_size(path: Path) -> tuple[int, int]:
    """（字节数, 文件数）。目录不存在就是 0。"""
    if not path.exists():
        return 0, 0
    total = 0
    count = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
            count += 1
    return total, count


def data_usage(root: Path | None = None) -> dict[str, dict[str, Any]]:
    """磁盘占用（设置栏的「数据」那一栏要显示的东西）。"""
    base = root or paths.home()
    targets: dict[str, Path] = {
        "database": paths.db_path(base),
        "fetch_cache": paths.data_dir(base) / "cache",
        "raw": paths.data_dir(base) / "raw",
        "text": paths.data_dir(base) / "text",
        "notes": paths.data_dir(base) / "notes",
        "run": paths.run_dir(base),
    }
    usage: dict[str, dict[str, Any]] = {}
    for name, path in targets.items():
        if path.is_file():
            size = path.stat().st_size
            usage[name] = {"bytes": size, "files": 1, "path": str(path)}
            continue
        size, count = _dir_size(path)
        usage[name] = {"bytes": size, "files": count, "path": str(path)}
    return usage


#: 允许清理的目标（**故意不含 raw / text / notes**：那些是资料本体，删了就没了）
CLEANABLE = {
    "fetch_cache": "抓取缓存（删了只是下次重新下载网页）",
    "run_tmp": "run/ 下的临时文件（诊断产物、临时库等）",
}


def cleanup(
    root: Path | None = None,
    target: str = "fetch_cache",
    *,
    older_than_days: float | None = None,
    now: float | None = None,
) -> dict[str, int]:
    """清理缓存类目录。返回删掉的（文件数, 字节数）。

    `older_than_days` 只删"够旧"的文件（按 mtime）：缓存的价值本来就随时间衰减，
    全清会让下次检索重新下载一遍。**资料本体永远不在可清理范围内**（见 CLEANABLE）。
    """
    base = root or paths.home()
    if target not in CLEANABLE:
        raise SettingsError(f"不允许清理：{target}（可清理的只有 {', '.join(CLEANABLE)}）")
    directory = paths.data_dir(base) / "cache" if target == "fetch_cache" else paths.run_dir(base)
    cutoff = None
    if older_than_days is not None:
        if older_than_days < 0:
            raise SettingsError("older_than_days 不能为负")
        cutoff = (now if now is not None else time.time()) - older_than_days * 86400
    files = 0
    size = 0
    for item in sorted(directory.rglob("*")):
        if not item.is_file():
            continue
        if target == "run_tmp" and item.name.startswith(("state.db", "scout.db")):
            continue  # 库不能删：那是全部数据
        if cutoff is not None and item.stat().st_mtime >= cutoff:
            continue  # 还"新鲜"，留着（缓存的价值随时间衰减，不是一滴不留）
        size += item.stat().st_size
        item.unlink(missing_ok=True)
        files += 1
    return {"files": files, "bytes": size}


def _database_pages(db: Any) -> int:
    """逻辑大小（页数 × 页大小）—— VACUUM 真正会收缩的是这个。

    为什么不能只看文件字节数：WAL 模式下写入先进 `-wal`，主库文件可能纹丝不动，
    于是"整理完文件没变小"会被误读成"整理没用"。
    """
    try:
        page_count = int(db.query_one("PRAGMA page_count")[0])
        page_size = int(db.query_one("PRAGMA page_size")[0])
    except Exception:  # pragma: no cover - 拿不到就退化成 0，不编数
        return 0
    return page_count * page_size


def optimize_database(db: Any, root: Path | None = None) -> dict[str, int]:
    """整理数据库：`VACUUM` + `PRAGMA optimize`，返回整理前后的字节数与页数。

    为什么需要它：删会话/删资料**不会**让 SQLite 变紧凑（页被标记空闲但仍占空间），
    所以"删了一堆会话但磁盘没变"是正常的，得显式整理。

    口径：**只整理、不删除任何数据** —— 与"资料本体拒绝清理"这条硬约定不冲突；
    同时给出文件字节数与逻辑页数（WAL 下两者会不一致，只报一个就会误导）。
    代价：**大库会短暂锁库**（几秒到几十秒），期间其它请求可能排队（有 busy_timeout 兜着）。
    """
    path = Path(db.path) if getattr(db, "path", None) is not None else paths.db_path(root)
    before = path.stat().st_size if path.is_file() else 0
    pages_before = _database_pages(db)
    db.execute("VACUUM")
    db.execute("PRAGMA optimize")
    after = path.stat().st_size if path.is_file() else 0
    pages_after = _database_pages(db)
    return {
        "before": before,
        "after": after,
        "saved": max(0, before - after),
        "pages_before": pages_before,
        "pages_after": pages_after,
        "page_saved": max(0, pages_before - pages_after),
    }

"""模型清单：**只列这把 key 真能用的模型**（先探 ``GET /models``，失败才回落到配置）。

为什么要探：写死一份清单必然会列出这个 key 用不了的模型 —— 用户选了它、下一轮 400，
比"只列一个模型"糟糕得多。探测**不产生生成费用**（只是一次 GET），所以每次启动都可以做；
失败就退回 `config.yaml` 里记着的候选 + 当前模型，并**如实说明没探到**。

探测结果的额外价值：本服务在模型对象里直接声明了真实思考档位
（``effort.supported_levels`` / ``default_level``），比在代码里猜靠谱，所以一并解析出来，
交给 :mod:`scholarscout.providers.thinking` 做并档映射。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from scholarscout.providers.thinking import DEFAULT_EFFORTS, DEFAULT_LEVEL

#: 探测超时（秒）。启动时同步调一次，**不能拖住界面**：探测是可选增强，超时就回落。
PROBE_TIMEOUT_S = 6.0

#: 同一进程内复用探测结果（界面每次开下拉不必都打一次网）。失败**不缓存**。
CACHE_TTL_S = 600.0

_CACHE: dict[str, tuple[float, list[ModelInfo]]] = {}


class ModelsProbeError(RuntimeError):
    """探测失败（网络/鉴权/接口不存在都会走到这里，由调用方回落）。"""


@dataclass(frozen=True)
class ModelInfo:
    """一个可用模型（``/models`` 的条目，缺字段就用官方默认值）。"""

    id: str
    label: str = ""
    #: 服务端声明的真实思考档位；没声明时按官方文档的 low/high/max
    efforts: tuple[str, ...] = DEFAULT_EFFORTS
    default_effort: str = "high"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label or self.id,
            "efforts": list(self.efforts),
            "default_effort": self.default_effort,
        }


def reset_cache() -> None:
    """测试用：清掉进程内缓存。"""
    _CACHE.clear()


def _efforts_of(item: dict[str, Any]) -> tuple[tuple[str, ...], str]:
    """从 ``effort`` 字段取真实档位（不认识的形状一律当"没声明"）。"""
    raw = item.get("effort")
    levels: list[str] = []
    if isinstance(raw, dict):
        listed = raw.get("supported_levels")
        if isinstance(listed, list):
            levels = [str(name).strip().lower() for name in listed if str(name).strip()]
        default = str(raw.get("default_level") or "").strip().lower()
    else:
        default = ""
    known = tuple(name for name in DEFAULT_EFFORTS if name in levels)
    if not known:
        return DEFAULT_EFFORTS, "high"
    return known, (default if default in known else known[-1])


def parse_models(body: Any) -> list[ModelInfo]:
    """解析 ``/models`` 的返回。认 ``{"data": [...]}`` 与 ``{"models": [...]}`` 两种形状。"""
    if isinstance(body, dict):
        listed = body.get("data")
        if not isinstance(listed, list):
            listed = body.get("models")
    else:
        listed = body
    if not isinstance(listed, list):
        raise ModelsProbeError("模型清单的形状不认识（既没有 data 也没有 models 数组）")
    out: list[ModelInfo] = []
    seen: set[str] = set()
    for item in listed:
        if isinstance(item, str):
            name = item.strip()
            entry: dict[str, Any] = {}
        elif isinstance(item, dict):
            name = str(item.get("id") or item.get("name") or "").strip()
            entry = item
        else:
            continue
        if not name or name in seen:
            continue
        seen.add(name)
        efforts, default_effort = _efforts_of(entry)
        out.append(
            ModelInfo(
                id=name,
                label=str(entry.get("name") or ""),
                efforts=efforts,
                default_effort=default_effort,
            )
        )
    if not out:
        raise ModelsProbeError("模型清单是空的（服务端没给任何模型）")
    return out


def probe(
    *,
    base_url: str,
    api_key: str,
    timeout_s: float = PROBE_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
) -> list[ModelInfo]:
    """调一次 ``GET {base}/models``。**不产生生成费用**；失败抛 :class:`ModelsProbeError`。"""
    url = base_url.rstrip("/") + "/models"
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_s), transport=transport) as client:
            response = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.TimeoutException as exc:
        raise ModelsProbeError(f"探测超时（{timeout_s}s）") from exc
    except httpx.HTTPError as exc:
        raise ModelsProbeError(f"探测请求失败：{exc}") from exc
    if response.status_code in (401, 403):
        raise ModelsProbeError(f"探测鉴权失败（{response.status_code}）：这把 key 不可用")
    if response.status_code == 404:
        raise ModelsProbeError("该端点没有 /models 接口（404）")
    if response.status_code >= 400:
        raise ModelsProbeError(f"探测返回 {response.status_code}：{response.text[:120]}")
    try:
        body = response.json()
    except ValueError as exc:
        raise ModelsProbeError("探测返回的不是 JSON") from exc
    return parse_models(body)


def discover(
    *,
    base_url: str,
    api_key: str,
    current: str,
    configured: tuple[str, ...] = (),
    timeout_s: float = PROBE_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """探测 + 合并，**永不抛异常**（失败就回落，并如实说明）。

    返回::

        {"available": [id...],        # 界面下拉的候选（探测结果 + 配置里记着的 + 当前模型）
         "models": [{id,label,efforts,default_effort}...],
         "current": "...", "source": "probe" | "config",
         "note": "…",                # 给人看的一句话（失败原因也在这里，不静默）
         "efforts": [...], "default_effort": "..."}
    """
    cache_key = f"{base_url.rstrip('/')}|{api_key[-6:]}|{current}"
    models: list[ModelInfo] = []
    note = ""
    source = "config"
    if use_cache:
        hit = _CACHE.get(cache_key)
        if hit is not None and time.monotonic() - hit[0] < CACHE_TTL_S:
            models = list(hit[1])
            source = "probe"
            note = "探测结果来自本次运行的缓存"
    if not models:
        if not api_key:
            note = "未配置 LLM_API_KEY，无法探测 /models（候选来自 config.yaml）"
        else:
            try:
                models = probe(
                    base_url=base_url, api_key=api_key, timeout_s=timeout_s, transport=transport
                )
                source = "probe"
                _CACHE[cache_key] = (time.monotonic(), list(models))
            except ModelsProbeError as exc:
                note = f"/models 探测失败（{exc}），候选回落到 config.yaml + 当前模型"

    return _assemble(models, current=current, configured=configured, source=source, note=note)


def _assemble(
    found: list[ModelInfo],
    *,
    current: str,
    configured: tuple[str, ...],
    source: str,
    note: str,
) -> dict[str, Any]:
    """把候选整理成界面要的形状（回落、当前模型保底、档位取当前模型那份）。"""
    models = found
    if not models:
        # 回落：配置里记着的候选 + 当前模型（去重、保序）
        names: list[str] = []
        for name in (*configured, current):
            cleaned = str(name or "").strip()
            if cleaned and cleaned not in names:
                names.append(cleaned)
        models = [ModelInfo(id=name) for name in names]

    ids = [item.id for item in models]
    # 当前模型必须在候选里（否则下拉会显示空 —— 用户会以为模型没配）
    if current and current not in ids:
        ids.insert(0, current)
        models = [ModelInfo(id=current), *models]

    picked = next((item for item in models if item.id == current), None)
    efforts = list(picked.efforts if picked else DEFAULT_EFFORTS)
    default_effort = picked.default_effort if picked else "high"
    return {
        "available": ids,
        "models": [item.as_dict() for item in models],
        "current": current,
        "source": source,
        "note": note,
        "efforts": efforts,
        "default_effort": default_effort,
        "default_level": DEFAULT_LEVEL,
    }


def fallback(
    *,
    current: str,
    configured: tuple[str, ...] = (),
    note: str = "",
) -> dict[str, Any]:
    """**不探测**直接回落（没配 key 时用）：候选 = config.yaml 记着的 + 当前模型。

    为什么单独开一个入口：没有 key 时探测必然 401，既浪费一次请求又只能拿到同一个回落结果 ——
    调用方先判 key 再决定要不要探测，`discover` 的 `api_key` 就始终是 `str`，不必强转。
    """
    return _assemble([], current=current, configured=configured, source="config", note=note)

"""OpenAI 兼容的对话模型客户端（DeepSeek / Ollama / 任何兼容端点同一套代码）。

- 出网走 httpx，**必须带超时**（规则 §3.6）
- 本层只负责"发请求、解析回复、把错误分类"；重试留给上层决定（规则 §3.5 的例外：
  模型调用不属于抓取层，重试策略在 runtime/loop.py 里按需加，当前不重试）
- 错误一律包成 :class:`LlmError` 并带 ``kind``，由 RPC 层映射成契约错误码
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

#: 错误分类 → 由 rpc/methods.py 映射成契约错误码
ERROR_KINDS = ("auth", "network", "timeout", "server", "bad_response", "rate_limited")


class LlmError(RuntimeError):
    def __init__(self, kind: str, message: str, *, http_status: int | None = None) -> None:
        if kind not in ERROR_KINDS:
            raise ValueError(f"未知错误类型：{kind!r}")
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # 原始 JSON 字符串，保持模型输出的样子

    def parsed_arguments(self) -> dict[str, Any]:
        if not self.arguments.strip():
            return {}
        try:
            parsed = json.loads(self.arguments)
        except json.JSONDecodeError as exc:
            raise LlmError(
                "bad_response", f"工具参数不是合法 JSON：{self.arguments[:120]}"
            ) from exc
        return parsed if isinstance(parsed, dict) else {"value": parsed}


@dataclass(frozen=True)
class ChatReply:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    #: 这条回复是不是从本地缓存读出来的（评测/调试要看得出"这次没花 token"）
    cached: bool = False

    @property
    def truncated(self) -> bool:
        """是否被 max_tokens 截断。

        推理模型的 reasoning token 也算在 completion 里，额度给小了会出现
        ``content`` 为空而 ``finish_reason == "length"`` —— 静默返回空串会让上层
        「按兜底模式生成」却不知道为什么（真机跑 E3 踩过）。
        """
        return self.finish_reason == "length"


class LlmClient(Protocol):
    """对话客户端协议（测试用桩实现它即可）。"""

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatReply: ...


class OpenAIChatClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_s: float = 60.0,
        thinking: bool = True,
        cache_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        #: 关掉思考＝请求体带 `thinking: {"type": "disabled"}`（实测 completion −78%）
        self.thinking = thinking
        #: 响应缓存目录（None＝不缓存）。交互命中率≈0，主要为**评测复跑**准备。
        self.cache_dir = cache_dir
        self._transport = transport
        self._client: httpx.Client | None = None

    # ---------------------------------------------------------------- 内部

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self.timeout_s),
                transport=self._transport,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if not self.thinking:
            # 关掉推理模型的思考：实测 completion 162 → 36（−78%），reasoning_content 归零。
            # 注意这是**质量与成本的取舍**，所以默认开着，由用户在设置栏里决定（见 config.LlmProfile.thinking）。
            payload["thinking"] = {"type": "disabled"}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    # ---------------------------------------------------------------- 调用

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatReply:
        url = self.base_url + "/chat/completions"
        payload = self._payload(messages, tools)
        # 可选缓存：**只对"同一请求原样重放"有效**（评测复跑、调试重跑）。
        # 交互场景几乎不可能命中（每轮内容都是新的），所以默认关着 —— 别把它当万能省钱药。
        hit = self._cache_read(payload)
        if hit is not None:
            return self._parse(hit, cached=True)
        try:
            response = self._http().post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise LlmError("timeout", f"模型请求超时（{self.timeout_s}s）：{exc}") from exc
        except httpx.HTTPError as exc:
            raise LlmError("network", f"模型请求失败：{exc}") from exc

        if response.status_code in (401, 403):
            raise LlmError(
                "auth", "模型鉴权失败（检查 LLM_API_KEY）", http_status=response.status_code
            )
        if response.status_code == 429:
            raise LlmError("rate_limited", "模型限流（429）", http_status=429)
        if response.status_code >= 500:
            raise LlmError(
                "server",
                f"模型服务端错误 {response.status_code}",
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            raise LlmError(
                "bad_response",
                f"模型返回 {response.status_code}：{response.text[:200]}",
                http_status=response.status_code,
            )

        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise LlmError("bad_response", "模型返回的不是 JSON") from exc
        self._cache_write(payload, body)
        return self._parse(body)

    # -------------------------------------------------------------- 响应缓存

    def _cache_path(self, payload: dict[str, Any]) -> Path | None:
        if self.cache_dir is None:
            return None
        # 键 = 请求体的内容哈希：模型、消息、工具声明、采样参数任一变化都不算命中
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return self.cache_dir / (hashlib.sha256(blob).hexdigest()[:32] + ".json")

    def _cache_read(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        path = self._cache_path(payload)
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _cache_write(self, payload: dict[str, Any], body: dict[str, Any]) -> None:
        path = self._cache_path(payload)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        except OSError:
            # 缓存写不进去不该让整轮对话失败
            return

    # ---------------------------------------------------------------- 解析

    def _parse(self, body: dict[str, Any], *, cached: bool = False) -> ChatReply:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LlmError("bad_response", "模型返回里没有 choices")
        raw_choice = choices[0]
        choice: dict[str, Any] = raw_choice if isinstance(raw_choice, dict) else {}

        raw_message = choice.get("message")
        message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}

        tool_calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls")
        if isinstance(raw_calls, list):
            for raw in raw_calls:
                if not isinstance(raw, dict):
                    continue
                raw_function = raw.get("function")
                function: dict[str, Any] = raw_function if isinstance(raw_function, dict) else {}
                tool_calls.append(
                    ToolCall(
                        id=str(raw.get("id") or ""),
                        name=str(function.get("name") or ""),
                        arguments=str(function.get("arguments") or ""),
                    )
                )

        raw_usage = body.get("usage")
        usage_raw: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        # 只留真正会用到的两个键（domain 对象不做 API 透传）
        usage: dict[str, int] = {}
        for key in ("prompt_tokens", "completion_tokens"):
            value = usage_raw.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                usage[key] = int(value)
        content = message.get("content")
        return ChatReply(
            content=str(content) if content is not None else "",
            tool_calls=tool_calls,
            finish_reason=str(choice.get("finish_reason") or "stop"),
            model=str(body.get("model") or self.model),
            usage=usage,
            cached=cached,
        )


def create_client(profile: Any) -> OpenAIChatClient:
    """按配置档建客户端；缺密钥直接抛 auth 错误，让上层给用户可读提示。"""
    api_key = profile.api_key() if hasattr(profile, "api_key") else None
    if not api_key:
        raise LlmError("auth", "未配置 LLM_API_KEY（放 .env 里；本地 Ollama 可随意填 ollama）")
    return OpenAIChatClient(
        base_url=profile.base_url,
        api_key=api_key,
        model=profile.model,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
        timeout_s=profile.timeout_s,
        thinking=bool(getattr(profile, "thinking", True)),
        cache_dir=_cache_dir_for(profile),
    )


def _cache_dir_for(profile: Any) -> Path | None:
    """开了 `llm_cache` 才给缓存目录（默认关：交互每轮内容都新，命中率≈0）。"""
    if not getattr(profile, "llm_cache", False):
        return None
    from scholarscout import paths

    return paths.data_dir() / "cache" / "llm"

"""模型回复的本地缓存：**只对"同一请求原样重放"有用**，默认关。

为什么默认关（诚实的技术判断，别把它当万能省钱药）：
- 交互场景里每一轮的 messages 都包含上一轮的新内容，请求体哈希必然不同 → 命中率≈0；
- 它真正有用的是**评测复跑与调试重跑**（同一批题、同一提示词，重跑一次就能省掉整轮费用）。

所以缓存的键必须是**整个请求体的哈希**：模型名、消息、工具声明、temperature、max_tokens、
thinking 开关里任何一项变了，都不能算命中 —— 否则"我改了参数但结果没变"会让人怀疑人生。
"""

from __future__ import annotations

import json
from pathlib import Path

from scholarscout.providers.llm import OpenAIChatClient


def make_client(cache_dir: Path | None, calls: list[dict]) -> OpenAIChatClient:
    """用猴补丁替换 httpx 调用，只验证缓存逻辑。"""
    client = OpenAIChatClient(
        base_url="https://example.test/v1", api_key="k", model="m", cache_dir=cache_dir
    )

    def fake_post(url: str, **kwargs: object):
        calls.append({"url": url, "payload": kwargs.get("json")})

        class Response:
            status_code = 200
            text = ""

            def json(self) -> dict:
                return {
                    "model": "m",
                    "choices": [{"message": {"content": "答案"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }

        return Response()

    client._http().post = fake_post  # type: ignore[method-assign]
    return client


def test_no_cache_dir_means_no_caching(tmp_path: Path) -> None:
    calls: list[dict] = []
    client = make_client(None, calls)
    client.chat([{"role": "user", "content": "hi"}])
    client.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 2, "没开缓存就不该省这次调用"


def test_second_identical_request_hits_the_cache(tmp_path: Path) -> None:
    calls: list[dict] = []
    client = make_client(tmp_path / "llm", calls)
    first = client.chat([{"role": "user", "content": "hi"}])
    second = client.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 1, "同样的请求不该打第二次 API"
    assert first.cached is False and second.cached is True
    assert second.content == first.content


def test_different_messages_do_not_collide(tmp_path: Path) -> None:
    calls: list[dict] = []
    client = make_client(tmp_path / "llm", calls)
    client.chat([{"role": "user", "content": "hi"}])
    client.chat([{"role": "user", "content": "另一个问题"}])
    assert len(calls) == 2


def test_changing_params_invalidates_the_cache(tmp_path: Path) -> None:
    """改了 thinking / max_tokens 就不算命中 —— 否则"改了参数结果没变"会让人怀疑人生。"""
    calls: list[dict] = []
    client = make_client(tmp_path / "llm", calls)
    client.chat([{"role": "user", "content": "hi"}])
    client.thinking = False
    client.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 2


def test_tools_changes_invalidate_the_cache(tmp_path: Path) -> None:
    calls: list[dict] = []
    client = make_client(tmp_path / "llm", calls)
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    client.chat([{"role": "user", "content": "hi"}], tools=tools)
    client.chat([{"role": "user", "content": "hi"}], tools=tools)
    client.chat([{"role": "user", "content": "hi"}])  # 不带工具＝另一个请求
    assert len(calls) == 2


def test_corrupt_cache_file_is_ignored(tmp_path: Path) -> None:
    """缓存文件坏了就当没有，不能让对话直接崩。"""
    calls: list[dict] = []
    client = make_client(tmp_path / "llm", calls)
    payload = client._payload([{"role": "user", "content": "hi"}], None)
    path = client._cache_path(payload)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{不是 JSON", encoding="utf-8")
    reply = client.chat([{"role": "user", "content": "hi"}])
    assert reply.content == "答案" and len(calls) == 1


def test_cache_file_holds_the_raw_response(tmp_path: Path) -> None:
    calls: list[dict] = []
    client = make_client(tmp_path / "llm", calls)
    client.chat([{"role": "user", "content": "hi"}])
    files = list((tmp_path / "llm").glob("*.json"))
    assert len(files) == 1
    body = json.loads(files[0].read_text(encoding="utf-8"))
    assert body["usage"]["prompt_tokens"] == 10

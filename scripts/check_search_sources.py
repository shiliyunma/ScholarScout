"""真机检查：逐个来源打一次真请求，看"能不能跟真实接口对上话"。

离线测试只证逻辑自洽（docs/05 §3.5）：调用一次真实 API、打印 HTTP 层的结果，
才能发现工具名、参数名、robots、限速、认证头这类只在真接口上暴露的坑。

用法（在项目根目录）：
    .venv/Scripts/python.exe scripts/check_search_sources.py                 # 只查配置里已启用的来源
    .venv/Scripts/python.exe scripts/check_search_sources.py --all           # 所有已实现的来源都试
    .venv/Scripts/python.exe scripts/check_search_sources.py --query "LLM agent" --limit 5

退出码：**免 key 来源全部成功**才返回 0；商用来源缺 key 不算失败（会打印 missing_api_key）。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scholarscout.config import load_config, load_dotenv  # noqa: E402
from scholarscout.fetch.http import HttpClient  # noqa: E402
from scholarscout.providers.base import Query  # noqa: E402
from scholarscout.providers.settings import COMMERCIAL_KEYS, IMPLEMENTED, api_key_for  # noqa: E402
from scholarscout.tools.research_search import resolve_wanted  # noqa: E402

KEYLESS = ("arxiv", "bing", "duckduckgo", "searxng")
#: 需要梯子/需显式开关的来源：不可用属预期，不算失败
CONDITIONAL = ("bing", "duckduckgo")


def build(name: str, http: HttpClient, searxng_base_url: str):
    if name == "arxiv":
        from scholarscout.providers.arxiv import ArxivProvider

        return ArxivProvider(http)
    if name == "bing":
        from scholarscout.providers.bing import BingProvider

        return BingProvider(http)
    if name == "duckduckgo":
        from scholarscout.providers.duckduckgo import DuckDuckGoProvider

        return DuckDuckGoProvider(http)
    if name == "searxng":
        from scholarscout.providers.searxng import SearxngProvider

        return SearxngProvider(http, base_url=searxng_base_url)
    if name == "tavily":
        from scholarscout.providers.tavily import TavilyProvider

        return TavilyProvider(http, api_key=api_key_for("tavily") or "")
    if name == "serper":
        from scholarscout.providers.serper import SerperProvider

        return SerperProvider(http, api_key=api_key_for("serper") or "")
    if name == "brave":
        from scholarscout.providers.brave import BraveProvider

        return BraveProvider(http, api_key=api_key_for("brave") or "")
    raise SystemExit(f"未知来源：{name}")


def run_via_tool(settings, query: str, limit: int) -> int:
    """走**真工具路径**（build_context → research_search → 入库），这才叫端到端。

    直接 new 一个 provider 只能证明"这个类能跑"，证明不了注册表、工具包装、
    入库与 provider_status 串起来是对的 —— 而坑恰恰都在这些接缝上。
    """
    import tempfile

    from scholarscout.app import build_context
    from scholarscout.runtime.toolctx import ToolContext
    from scholarscout.tools.research_search import research_search

    with tempfile.TemporaryDirectory(
        prefix="scout-search-check-", ignore_cleanup_errors=True
    ) as tmp:
        base = Path(tmp)
        cfg = replace(
            load_config(ROOT / "config.yaml"),
            data_dir=base / "data",
            db_path=base / "state.db",
            search_cfg=settings,
        )
        app = build_context(config=cfg, db_path=cfg.db_path)
        tool_ctx = ToolContext(db=app.db, config=app.config, http=app.http, llm=app.llm)
        print(f"[via-tool] 配置来源：{list(settings.providers)}（auto 会展开）")
        try:
            result = research_search(tool_ctx, query, limit=limit)
            status = result["data"]["provider_status"]
            for row in status:
                mark = "OK  " if row.get("ok") else "FAIL"
                print(f"[{mark}] {row['provider']}: n={row['n']} {row.get('error') or ''}")
            items = result["data"]["items"]
            print(f"[via-tool] 入库并返回 {len(items)} 条")
            for item in items[:3]:
                print(
                    f"  - {item['source_id']} [{item['source_type']}] "
                    f"tier={item['tier']} {item['title'][:50]}"
                )
            total = app.db.scalar_int("SELECT count(*) FROM sources")
            print(f"[via-tool] 库里 sources 共 {total} 条")
            ok = len(items) > 0 and total == len(items)
            print("[via-tool] 结果：" + ("通过（真检索真入库）" if ok else "不通过"))
            return 0 if ok else 1
        finally:
            # Windows 上文件被占着就删不掉临时目录：库与出网客户端都要显式关
            app.db.close()
            if app.http is not None:
                app.http.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="educational agent LLM")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--all", action="store_true", help="所有已实现来源都试（默认只试配置里启用的）"
    )
    parser.add_argument("--only", default="", help="逗号分隔，只试这些来源")
    parser.add_argument(
        "--searxng-base-url", default="", help="覆盖 search.searxng_base_url（自检用）"
    )
    parser.add_argument(
        "--via-tool",
        action="store_true",
        help="走真工具路径（build_context → research_search → 入库），端到端验一次",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / "config.yaml")
    settings = config.search_cfg
    if args.searxng_base_url:
        settings = replace(settings, searxng_base_url=args.searxng_base_url)
    requested = (
        [name.strip() for name in args.only.split(",") if name.strip()]
        if args.only
        else (list(IMPLEMENTED) if args.all else list(settings.providers))
    )
    # 关键：用**工具那套**解析（把 auto 展开成真来源）。
    # 否则 `providers: ["auto"]` 会被当成一个叫 auto 的来源：查不到 key → 跳过 → 报"成功"，
    # 而 arxiv 根本没跑 —— 脚本会永远绿着骗人（真机上就这么骗过一次）。
    names = resolve_wanted(settings, requested)
    print(f"解析后的来源：{', '.join(names) if names else '（无）'}")
    if args.via_tool:
        return run_via_tool(settings, args.query, args.limit)

    http = HttpClient(
        respect_robots=True,
        cache_dir=ROOT / "run" / "fetch_cache",
        cache_ttl_hours=config.fetch_cfg.cache_ttl_hours,
    )
    failures: list[str] = []
    # 预期可用的集合：auto 解析出来的 + 显式点名的（抓取类除外，它们要开关）
    expected = set(settings.auto_names()) | (set(names) - set(CONDITIONAL) if args.only else set())
    try:
        for name in names:
            if name not in IMPLEMENTED:
                print(
                    f"[FAIL] {name}: not_implemented（未知或本版未实现；可用见 providers/settings.py）"
                )
                failures.append(name)
                continue
            if name == "searxng" and not settings.searxng_base_url.strip():
                print(f"[skip] {name}: missing_config（未配 search.searxng_base_url）")
                if name in expected:
                    failures.append(name)
                continue
            if name in CONDITIONAL and not settings.allow_searchpage_scraping:
                print(
                    f"[skip] {name}: blocked_by_robots（要抓需开 search.allow_searchpage_scraping）"
                )
                continue
            if name in COMMERCIAL_KEYS and api_key_for(name) is None:
                print(f"[skip] {name}: missing_api_key（未配 .env，属预期）")
                continue
            print(f"\n===== {name} =====")
            try:
                cards = build(name, http, settings.searxng_base_url).search(
                    Query(text=args.query, limit=args.limit)
                )
            except Exception as exc:  # 真机检查：任何异常都要原样打印出来
                print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
                if name in expected:
                    failures.append(name)
                continue
            print(f"命中 {len(cards)} 条")
            for card in cards[:3]:
                year = f" ({card.year})" if card.year else ""
                print(f"  - {card.title[:70]}{year}\n    {card.url}")
                if card.snippet:
                    print(f"    {card.snippet[:100]}")
            if not cards and name in expected:
                failures.append(name)
                print("[FAIL] 预期可用的来源返回 0 条（要看是解析器过期还是被拦）")
    finally:
        http.close()

    print("\n===== 汇总 =====")
    if not names:
        print("[FAIL] 一个来源都没解析出来 —— 配置有问题（不能算通过）")
        return 1
    if failures:
        print(f"预期可用的来源有问题：{', '.join(failures)}")
        return 1
    print(f"预期可用的来源全部成功（{len(names)} 个）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

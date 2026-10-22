# How Hermes Agent and an AIHot-style aggregator solve "general search"

Design precedent for a report/thesis section on "work related to the acquisition layer". Both were read
from source, and both refuse the browser-as-search-engine idea.

## Hermes Agent — a web-search provider registry

`plugins/web/` + `agent/web_search_registry.py`. Twelve pluggable backends:
`firecrawl, parallel, tavily, perplexity, exa, searxng, brave_free, ddgs, keenable, keyless_mcp, xai`.

Resolution order (documented in the registry's own docstring):

1. config `web.search_backend` / `web.extract_backend`, then shared `web.backend`
2. the single capability-eligible provider that is **registered AND available**
3. legacy preference walk, **paid first**: firecrawl → parallel → tavily → perplexity → exa →
   searxng → brave-free → ddgs (filtered by availability, so a paid setup never silently downgrades)
4. keyless free-tier walk, last resort
5. `None` — the tool tells the user to configure one (`hermes tools`)

Points worth copying:

- **Search and extract are separate backends** with separate resolution; an extract-only provider
  configured as `web.search_backend` falls through automatically. (Same reason to keep
  `research.search` and `source.fetch` apart in an app.)
- **Self-hosted SearxNG is a first-class provider**, not a hack: "user-hosted instance,
  `GET /search?format=json`", configured with `SEARXNG_URL=http://localhost:8080`, help link pointing
  at the public instance list (searx.space). Matches the recipe in `references/searxng-selfhost.md`.
- **The keyless scrape-style backend (`ddgs`) runs in a subprocess** (`_search_worker` + `subprocess` +
  `concurrent.futures`) — isolation plus a hard timeout for a client nobody trusts to be stable.
- The shared HTTP helper is deliberately thin (`httpx.get` + timeout + normalized error objects):
  **no robots handling, no rate limiting, no cache.** If your project adds those, it is a real
  difference worth stating (and defending) in a write-up rather than assuming the reference does it.
- **Browsers are a separate tool family** (`browser_tool`, `browser_tool_cloud`, `browser_cdp_tool`,
  `browser_lightpanda`, `browser_supervisor*`, `browser_extension_router`) — multiple browser backends
  for *acting on pages*, never a search backend.

## AIHot-style aggregator — no general search at all

A curated **source registry with one adapter per source kind** instead of a query engine. Six kinds
(`packages/backend/src/sources/config-keys.ts`): `rss`, `web_list`, `json_list`, `x_search`,
`mp_account`, `external`.

- **No browser dependency in the project at all** (no playwright/puppeteer/chromium in any
  `package.json`). JS rendering is **outsourced to a paid reader service per call**. Paid crawler-style
  sources (a social platform API, a WeChat-article API) work the same way: per-request billing.
- **Config keys are a whitelist.** An unknown key is rejected at save time and the fetch fails with a
  visible reason — it never silently falls back to a generic parser. (App equivalent: an unknown
  provider name must surface as `unknown_provider`, not be ignored.)
- **Preview before create:** trial-fetch a source and inspect the first N items (title/link/date) to
  confirm you are capturing articles rather than navigation, before it goes into production.
- **Productionization worth borrowing:** a retry backoff ladder per source; **paid calls carry receipts
  and a budget** (dedicated budget-exceeded / provider-rejected error types) so cost is accountable; a
  global collection switch; per-source failure reasons visible in the admin UI.

## Convergent conclusions (the defensible design argument)

1. Treat "get material from outside" as a **swappable backend plus credentials/config**, not as "write
   a scraper".
2. Keep **search** and **extraction** separate.
3. Use a **local metasearch service** (or a keyed commercial API) for general search; outsource JS
   rendering to a paid reader for extraction.
4. Do not put a browser on the retrieval path.
5. Make every backend's unavailability **explicit and attributed**, and budget the paid ones.

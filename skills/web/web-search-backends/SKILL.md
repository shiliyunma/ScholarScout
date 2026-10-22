---
name: web-search-backends
description: "Use when building a web-search layer for an agent app."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web-search, search-api, searxng, provider-registry, robots, cache, keyless, china-network]
    related_skills: [blocked-page-recovery, china-mirror-installs, tool-calling-agent-runtime]
---

# Web-search backends: measure reachability, then choose the route

Class of task: an agent or app needs "general web search" (plus, separately, page extraction) and you
must decide what actually serves it **on this machine**: an academic/official API, a self-hosted
metasearch instance, a commercial search API, or scraping a result page. Deliverable: a provider layer
that returns normalized hits, states honestly which backends are unavailable and why, and whose
self-check cannot pass while doing nothing.

**The route must follow measurement, not preference.** Run one GET per candidate and record status,
latency and *result quality* before designing anything. `blocked`, `robots-disallowed`, and
`200-but-useless` are three different verdicts needing three different remedies — and only the last one
is invisible to a status-code check.

## Procedure

1. **Probe candidates before designing** (minutes that save a rewrite):
   ```bash
   curl -s -o /dev/null -w "%{http_code} %{time_total}\n" --max-time 8 "<candidate-url>"
   curl -s "<host>/robots.txt" | grep -i -A2 "disallow" | head
   ```
   Then parse one real result page and count genuine hits. Two engines can both answer 200 while one
   returns nothing your parser can use.

2. **Classify the source, then pick the transport:**

   | Need | Route | What it costs |
   |---|---|---|
   | Academic / domain API | the provider's own API | free; per-host rate limit; watch language coverage |
   | Zero-key general web | self-hosted metasearch (SearxNG) | a local service; see `references/searxng-selfhost.md` |
   | General web, no local service | commercial search API (keyed) | money or a free quota; key in `.env` |
   | Result-page scraping | last resort | robots-disallowed on the big engines **and** poor quality |

   JS-rendered *result pages* are not a reason to pick a different search route — that is an
   extraction problem, and it belongs in step 8.

3. **One module per provider, one shared card normalizer.** Each provider returns the same
   `{url, title, snippet, year, source_type}` shape; the shared normalizer unwraps redirect shells
   (`/ck/a?...&u=a1<base64>`, `/l/?uddg=<pct-encoded>`), strips tags, and drops non-content hits
   (dictionaries, translation, encyclopaedia stubs, doc-farm and product pages). Put the drop-list in
   one place — every provider inherits it.

4. **Make availability explicit and machine-readable.** Build the provider set at call time; an
   unavailable backend is reported, never skipped silently:
   `missing_api_key: <ENV>` · `missing_config: <config key>` · `blocked_by_robots` ·
   `not_implemented` · `unknown_provider`. A default of `providers: ["auto"]` means "only what is
   usable right now" — auto must exclude anything needing an absent key/instance, and must exclude
   routes that would just time out, or every search pays that latency.

5. **Separate search from extraction.** Search returns cards; extraction fetches the body. Different
   backends, different caching rules, different failure modes — one call for both couples a cheap
   metadata query to an expensive download.

6. **Cache discipline** (this is where a backend "mysteriously never finds anything" is born):
   - write to the cache only on `200` **and** non-empty body — caching an empty 200 pins one transient
     moment into a fact for the whole TTL;
   - **search requests bypass the cache entirely** (a search is a point-in-time question; page
     extraction is what caching is for);
   - a POST cache key must include a body hash, or two different queries collide.

7. **Verify through the real path.** A self-check script is only worth having if it exercises the same
   resolution the app does (config → registry → provider → store). Resolve config through the app's own
   resolver; treat an unknown or unresolvable name as a **hard failure**.

8. **Do not reach for a browser to search** (see pitfalls). Keep browser work for extracting a page
   that needs JS or a session.

## Pitfalls

- **Picking the route by preference, then discovering it is blocked.** Probe first; when you have to
  retract a route, say what you actually measured.
- **A 200 status is not a usable result set.** An engine can answer 200 with dictionary/nav results for
  a query whose first word stands alone, and a metasearch engine can answer `200` with `results: []`
  plus an empty `unresponsive_engines` list while it is still starting up. Inspect hits, not statuses.
- **Silently skipping an unavailable backend** conflates "no results" with "not configured"; the model
  then retries the same query until its tool budget burns out and starts inventing. Report a status row
  per backend with the reason (step 4).
- **Caching a transient empty/failure response.** One misconfigured minute becomes "this source has
  nothing" for hours, with nothing in the logs to say so — and a direct curl of the same backend
  answers fine, which sends you debugging the parser. Only cache 200-and-non-empty; bypass the cache
  for search (step 6).
- **Believing a browser will fix search quality.** Driving a real headless browser at a search engine
  returned the *same* junk for the same query, and a CAPTCHA-walled engine stayed walled with a real
  session — a browser adds a heavy runtime (and a new dependency) without adding results, and cannot
  run in a headless evaluation harness. Browser = extraction, not retrieval.
- **Scraping a search result page against its robots.txt.** The major engines disallow their search
  path. Make it an explicit opt-in switch, document that turning it on adds the host to the robots
  exemption list, and never ship it as the default.
- **A self-check that verifies nothing** (the worst failure mode, because it reports green): a checker
  that reads the config's sentinel value (`auto`) as if it were a backend name finds no key for
  "auto", skips every real backend, and exits 0. Checkers must resolve config through the same code
  path the app uses, and any name that resolves to nothing must fail loudly.
- **Judging a backend from one query, or from a forced single-backend call.** An engine can return
  nothing for one query shape and work fine for others, and an engine that produces hits when
  explicitly named can contribute zero inside the aggregate engine set. Validate with normal queries
  **and** the aggregate — that is what production executes.
- **Forgetting the local service's startup time.** A freshly restarted metasearch container needs
  ~15-20 s before engines load; querying earlier looks exactly like "all engines are dead".

## Files

- `references/searxng-selfhost.md` — standing up a self-hosted SearxNG that actually returns results:
  compose + the settings.yml keys that must change, JSON output, the keep-only-vs-enable trap,
  China-network Docker mirror pull, per-engine verdicts, and how to verify.
- `references/hermes-aihot-precedents.md` — how Hermes Agent and an AIHot-style aggregator solve the
  same problem (provider registry with paid-first/keyless-last precedence; source table + adapters with
  paid rendering instead of a browser). Design precedent for a report or thesis.

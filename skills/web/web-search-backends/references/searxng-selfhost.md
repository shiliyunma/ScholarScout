# Self-hosted SearxNG that actually returns results

SearxNG is the reference zero-key general-search route: it runs locally, owns the engine list, and
returns JSON — so nothing in your app has to parse a result page. It is also the route Hermes Agent
itself uses for keyless search (`SEARXNG_URL`, `GET {base}/search?format=json`), so a working instance
also serves any Hermes-side tooling that expects one.

## 1. Get the image (China networks: Docker Hub is unreachable)

```bash
# probe candidate mirrors first: 401/200 = reachable, 000 = dead
for m in docker.m.daocloud.io docker.1ms.run docker.1panel.live hub.rat.dev; do
  echo "$m -> $(curl -s -o /dev/null -w '%{http_code}' --max-time 8 https://$m/v2/)"
done

# pull via a reachable mirror, then retag to the official name so compose finds it
docker pull docker.m.daocloud.io/searxng/searxng:latest
docker tag  docker.m.daocloud.io/searxng/searxng:latest searxng/searxng:latest
```

If `docker compose` reports `failed to connect to the docker API at
npipe:////./pipe/dockerDesktopLinuxEngine ... The system cannot find the file specified`, the engine is
simply not running yet: launch Docker Desktop and poll `docker info` until it succeeds (~10-60 s) before
retrying compose.

## 2. Compose file

```yaml
services:
  searxng:
    image: searxng/searxng:latest
    container_name: <app>-searxng
    ports: ["8080:8080"]
    volumes: ["./settings.yml:/etc/searxng/settings.yml:ro"]
    environment:
      - SEARXNG_BASE_URL=http://localhost:8080/
      - SEARXNG_SECRET=<local-only-change-me>
    restart: unless-stopped
```

`restart: unless-stopped` keeps it up across reboots **only if Docker Desktop itself is running at
boot** — tell the operator that, or every reboot looks like "search broke".

## 3. settings.yml — the things that decide whether it works

```yaml
use_default_settings:
  engines:
    keep_only: [<engine-a>, <engine-b>]   # narrow to engines reachable from THIS network

engines:                                 # ← required: keep_only only KEEPS, it does not ENABLE
  - name: <engine-a>
    disabled: false
  - name: <engine-b>
    disabled: false

server:
  secret_key: "<local-only-change-me>"
  limiter: false                         # local self-use: the anti-abuse limiter rejects rapid queries

search:
  formats: [html, json]                  # JSON IS NOT ON BY DEFAULT — without it the API 400s/403s
  safe_search: 0
  autocomplete: ""
```

1. **`search.formats` must include `json`.** Default is html-only; the API path then fails even though
   the web UI works fine.
2. **`keep_only` only keeps, it does not enable.** Engines kept from the default table stay `disabled`
   unless you also list them under top-level `engines` with `disabled: false`. Symptom: `/config`
   reports **0 enabled engines**, an aggregate query returns `results: []`, yet a query that names an
   engine explicitly (`&engines=<name>`) returns hits — because an explicitly named engine is
   force-run. That "single engine works, aggregate returns nothing" split is this trap, not a bug in
   your parser.
3. **`keep_only` must match reality on this network.** The default engine table is full of hosts that
   are unreachable or bot-walled in some regions; leaving them enabled costs a timeout on every query.

## 4. Verify it, don't assume it

```bash
# authoritative: what does the container itself consider enabled?
curl -s http://localhost:8080/config | python -c "import sys,json;d=json.load(sys.stdin);print([e['name'] for e in d['engines'] if e.get('enabled')])"

# accepted JSON contract
curl -s "http://localhost:8080/search?q=<normal query>&format=json" \
  | python -c "import sys,json;d=json.load(sys.stdin);print('hits',len(d['results']),'unresponsive',d.get('unresponsive_engines'))"

# per-engine attribution (which engines actually contribute to the aggregate)
curl -s "http://localhost:8080/search?q=<normal query>&format=json" \
  | python -c "import sys,json,collections;d=json.load(sys.stdin);print(collections.Counter(r.get('engine') for r in d['results']))"
```

- Count **enabled engines** from `/config`; "the file looks right" is not evidence.
- After a restart, wait **~15-20 s** before querying. Too early: `results: []` with an empty
  `unresponsive_engines` — indistinguishable from every engine being dead.
- Report per-engine contributions, not just the total: an enabled engine can contribute 0.

## 5. Engine verdicts (a starting hypothesis, MEASURE YOUR OWN)

Observed from a mainland-China network — treat as a shortlist to test, never as fact:

| Engine | Behaviour |
|---|---|
| 360search | returns hits |
| sogou | returns hits (best Chinese coverage of the set) |
| bing | hits when force-named; contributed 0 inside the aggregate on the same query |
| mwmbl | hits once, then timeouts — flaky, slows the whole query |
| baidu | CAPTCHA (also CAPTCHA with a real browser session) |
| quark / chinaso / mojeek | crash / empty / empty |
| google, duckduckgo, brave, wikipedia, wikidata | unreachable from this network |

Engine availability is per-query too: a query whose **first word stands alone as a dictionary entry**
can make Bing-family engines return dictionary/navigation results instead of organic ones. Judge an
engine with normal queries, and never promote or retire one on a single query.

## 6. Consuming it from the app

```
GET {base}/search?q=<query>&format=json   →  {results: [{url, title, content, engine}, ...]}
```

- SearxNG is itself the metasearch layer: no HTML parsing, no redirect unwrapping.
- Treat the local service as optional: when it is down, report the backend as unavailable (see the
  parent skill's status discipline) rather than failing the whole search.
- Because it is a local service, it must exist on any machine that runs the app headless (evaluation
  harnesses included) — an app-level `auto` provider list keeps the app working when it does not.

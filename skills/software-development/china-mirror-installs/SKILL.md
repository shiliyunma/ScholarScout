---
name: china-mirror-installs
description: "Use when an install or download stalls on a China network."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [china, mirrors, install, network, pip, uv, npm, playwright, electron, windows, throughput]
---

# China-network installs: probe the mirror, then install or bypass

Class of task: an install, update, or download on a China-connected machine is slow, hangs, or dies
partway — dependency resolution inside an installer, a browser/Electron payload, a git clone, model
weights. Deliverable: the thing actually installed, plus the measured reason it was slow and the exact
command that avoids it next time.

The default failure mode is not "no mirror exists" — it is recommending a mirror that does not carry
the file you need. Probe first, advise second.

## Procedure

1. **Get the real URL and byte count the step fetches before advising anything.**
   Read the shipped script or binary, not the docs: `grep -n -i "<tool>" install.ps1`, and for a GUI
   installer extract strings (ASCII + UTF-16) from the .exe. You need the host, the path, and the size,
   because "slow" only becomes actionable once you can compute minutes = size / measured throughput.

2. **Prove the mirror serves YOUR file for YOUR platform.** Three probes, all cheap:
   - exact URL: `curl -sIL --max-time 25 "<mirror-url>" | grep -iE "HTTP/|content-length"` → want 200/206
     with a plausible size.
   - inventory: mirrors expose a JSON directory listing; list it and grep for your platform's filename
     (`curl -s "<mirror-dir>/" | python -c "import sys,json;[print(x['name']) for x in json.load(sys.stdin)]"`).
   - **A directory that answers 200 proves nothing.** Package mirrors sync per-platform and often drop
     one silently: the npm-China mirror's Playwright tree carries only `linux64/` and `mac-arm64/` under
     `builds/cft/<version>/`, so pointing `PLAYWRIGHT_DOWNLOAD_HOST` at it 404s on Windows.
   Prefer the mirror keyed by the **artifact** over the mirror keyed by the **consumer** when the
   consumer changed its path layout — download Chrome-for-Testing from the `chrome-for-testing` mirror
   rather than expecting the `playwright` mirror to have the new layout.

3. **Set mirror/proxy variables in the PARENT shell, then launch.** GUI installers spawn child
   processes that inherit the parent environment:
   ```powershell
   $env:UV_DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
   $env:HTTPS_PROXY      = "http://127.0.0.1:7890"
   & "D:\path\Setup.exe"
   ```
   Setting them inside the child's own script is too late. Exporting them in a shell you then close is
   also too late.

4. **Know which config each ecosystem actually reads.**

   | Tool | Give it | It ignores |
   |---|---|---|
   | pip | `pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple` (`%APPDATA%\pip\pip.ini`) | — |
   | uv | `UV_DEFAULT_INDEX` env, or `%APPDATA%\uv\uv.toml` with `[[index]] url="..."` + `default = true` | **`pip.conf` and `PIP_INDEX_URL`** — uv documents that it never reads pip-specific config |
   | npm / Electron | `.npmrc` → `registry.npmmirror.com`; `ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/` | — |
   | Playwright browsers | `PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST` (per-browser, wins) / `PLAYWRIGHT_DOWNLOAD_HOST`, `PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT` (ms), `PLAYWRIGHT_BROWSERS_PATH` | — |
   | GitHub repo source | `codeload.github.com/<owner>/<repo>/zip/refs/heads/<branch>` instead of `git clone` | — |
   | GitHub **release assets** | prefix proxy: `https://ghproxy.net/https://github.com/<owner>/<repo>/releases/download/<tag>/<file>` (`codeload` carries source zips, not releases). Verified dead on the same network: `ghfast.top`, `ghproxy.cc`, `hub.gitmirror.com`, `cdn.jsdelivr.net/gh` — they resolve to nothing, they do not 404 | plain `github.com/.../releases/...` (dial timeout on :443) |
   | Go toolchain + docs | `golang.google.cn` — mirrors `go.dev` docs *and* serves the zip/msi when `go.dev`/`golang.org` TLS-time-out; then `go env -w GOPROXY=https://goproxy.cn,direct` for modules, or every `go get` stalls | `go.dev` / `golang.org` on a blocked network |
   | ModelScope direct file | `https://modelscope.cn/models/<org>/<repo>/resolve/master/<file>` — serves any repo file (verified for a 635 MB GGUF). Probe the **first bytes for the format magic** (`curl -r 0-15` and look for `GGUF`/`PK`/`\x7fELF`) rather than trusting `content-length` alone | the Hub's file CDN |
   | Model weights | **`hf-mirror.com`** when the checkpoint exists only on the Hub — the Hub's **API** stays reachable while its **file CDN** does not, so enumerate via the API and fetch each `resolve/main/<path>` from the mirror (measured 21.6 MB/s); modelscope.cn when the checkpoint is mirrored there | `snapshot_download()` / `hf_hub_download()`, **even with `HF_ENDPOINT` set** — they reject a mirror's responses as "not on huggingface.co"; see `references/huggingface-mirror-downloads.md` |
   | Docker images | a China registry mirror, then retag: `docker pull docker.m.daocloud.io/<repo>:<tag> && docker tag docker.m.daocloud.io/<repo>:<tag> <repo>:<tag>` so `compose`/`run` still finds the official name | `registry-1.docker.io` itself — unreachable on a China network, and the failure reads as a dial timeout to `162.125.x.x:443` |

   Consequence: a machine with pip already pointed at Tsinghua can still crawl, because the installer
   shells out to **uv**. Ask which tool performs the download, then configure that tool.

   **Docker registries are probed differently from file mirrors** — a healthy one answers *401*
   (auth required), a dead one answers *000*:
   ```bash
   for m in docker.m.daocloud.io docker.1ms.run docker.1panel.live hub.rat.dev; do
     echo "$m -> $(curl -s -o /dev/null -w '%{http_code}' --max-time 8 https://$m/v2/)"
   done
   ```
   Never conclude "the mirror has no such image" from one mirror: a retagged pull is the cheapest
   verification, and it is the step that actually proves coverage.

5. **Measure throughput; never report "slow" as an adjective.**
   ```bash
   curl -sL --max-time 25 -r 0-8388607 -o "$LOCALAPPDATA/Temp/spd.bin" \
     -w "code=%{http_code} got=%{size_download} speed=%{speed_download} t=%{time_total}\n" "<url>"
   ```
   **On Windows/git-bash `-o /dev/null` silently yields 0 bytes and exit 23** (native curl cannot write
   the MSYS path) — it looks exactly like a dead 0 KB/s server and sends you chasing the wrong problem.
   Always write to a real file under `$LOCALAPPDATA/Temp` (or `-o NUL`), then delete it. Report the two
   speeds side by side: that is what justifies the mirror and predicts the ETA.
   **This is one instance of a general rule: any *path* argument handed to a native binary must be a
   native path.** `py -3.12 -m venv /d/proj/venv` fails with *No such file or directory* where
   `D:/proj/venv` succeeds; use forward-slash native paths (`D:/x/y`) — they work in both shells and
   avoid escape mangling.

6. **Always include the no-download path.** For any large payload, name the option that skips it:
   - pre-seed the tool's cache directory with the artifact plus its completion marker, so the installer
     finds it already installed;
   - or point the tool at an already-present binary (Hermes' browser tool honors
     `AGENT_BROWSER_EXECUTABLE_PATH`, which makes it skip the ~306 MB Chromium payload).
   Say plainly which of these you verified and which is a layout-mirroring inference.

7. **When the target machine is not this one**, the deliverable is copy-pasteable PowerShell/batch the
   user runs themselves — one block per remedy, paths in `$env:LOCALAPPDATA`-style form, no "I ran it
   for you". Bind remedy order to cost: mirror-pre-seed first, proxy second, skip-the-download third.

## Pitfalls

- **Reading a Docker daemon error as a network error.** `docker compose up` failing with `failed to
  connect to the docker API at npipe://.../dockerDesktopLinuxEngine` means the daemon is not running,
  not that a registry is blocked — launch Docker Desktop and poll `docker info` until it answers before
  touching mirrors, or you will debug a registry nobody was listening to.
- **Recommending a mirror before probing it.** If you have to retract, retract explicitly and state what
  you actually tested — an unverified recommendation costs the user a 200 MB failed download.
- Recommending a host override because the mirror is famous. Popularity is not coverage; check the
   inventory for the platform subdirectory.
- Guessing a CDN URL's form. Hand-building `.../dbazure/download/...` style paths 400s even when the file
   exists; use the plain storage-bucket form, or take the URL from the tool's own bundle.
- Numbering the installer's stages yourself when explaining "which step is slow". Manifest order is
   authoritative; your own numbered summary is not. When the user cites "the Nth step", get its title and
   map it explicitly, or you answer about the wrong stage.
- Treating the docs as the spec for prerequisites and flags. The shipped script is the spec: it is where
   you learn the real wall-clock caps, which flags exist on this platform, and which log records the
   failure.
- Forgetting that a wall-clock cap, not the network, is what killed the step. A step that "took forever"
   and produced no error is usually a timeout ceiling being hit; find the constant and its env override.
- **A 5xx from a registry mirror is not "the image does not exist".** Mirrors fail per-image and per-layer:
   in one session the same daemon pulled six images cleanly from a mirror and got **502** on a seventh,
   which then came down fine from the next mirror in the probe list. So on a 5xx, move to the next mirror and
   retag back to the official name (`docker tag <mirror>/<repo>:<tag> <repo>:<tag>`) so `compose` still finds it
   — do not conclude the image is unavailable, and do not start rewriting the compose file's image names.
   Keep the three codes straight: **000 = host dead**, **401 = healthy, auth required**,
   **5xx = this mirror cannot serve this artifact (try another)**.

## Files

- `references/playwright-browser-download.md` — what `npx playwright install chromium` downloads, verified
  mirror inventories and URL forms, the pre-seed recipe, and the env vars that steer it.
- `references/hermes-windows-installer.md` — Hermes-Setup.exe / install.ps1 internals: real prerequisites,
  the stage manifest and what each stage downloads, the 600 s cap on the browser payload, and where the
  logs are. Product docs stay in the bundled `hermes-agent` skill; this is install/network depth only.
- `references/huggingface-mirror-downloads.md` — the Hub's API-reachable / CDN-blocked asymmetry, why
  `snapshot_download()` rejects `HF_ENDPOINT` mirrors and what to do instead, non-flat repo layouts,
  offline flags, the ASCII-path venv + `--system-site-packages` trick for reusing an existing torch, and
  `USE_TF=0`. Read this before any model-weight download that is not on ModelScope.

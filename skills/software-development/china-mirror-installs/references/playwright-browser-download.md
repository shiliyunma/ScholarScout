# Playwright / Chrome-for-Testing behind a slow or blocked network

## What the command actually pulls (Windows)

`npx playwright install chromium` downloads two payloads and unpacks them into the browsers path
(`%LOCALAPPDATA%\ms-playwright` on Windows, `~/Library/Caches/ms-playwright` on macOS,
`~/.cache/ms-playwright` on Linux):

| Artifact | Size | Extracts to (inside the versioned dir) |
|---|---|---|
| `chrome-win64.zip` | ~192 MB | `chrome-win64/` (single top-level dir) |
| `chrome-headless-shell-win64.zip` | ~115 MB | `chrome-headless-shell-win64/` |

Installed layout — mirror this exactly when pre-seeding:

```
%LOCALAPPDATA%\ms-playwright\
  chromium-<revision>\                   chrome-win64\  INSTALLATION_COMPLETE  DEPENDENCIES_VALIDATED
  chromium_headless_shell-<revision>\    chrome-headless-shell-win64\  INSTALLATION_COMPLETE
  ffmpeg-<rev>\  winldd-<rev>\           (whole tree ≈ 690 MB)
```

## Version mapping (do not guess it)

`node_modules/playwright-core/browsers.json` in the project gives both numbers:

- `chromium.revision` → the directory suffix (`chromium-1234`)
- `chromium.browserVersion` → the CDN path segment (`builds/cft/151.0.7922.34/...`)

Modern Playwright fetches Chrome-for-Testing under `builds/cft/<browserVersion>/<platform>/...`; the older
`builds/chromium/<revision>/...` layout is what a stale mirror may still carry.

## URL + mirror facts (probe them again before relying on them)

- Official mirrors the bundle tries: `https://cdn.playwright.dev/dbazure/download/playwright`,
  `https://playwright.download.prss.microsoft.com/dbazure/download/playwright`, and the plain storage
  bucket `https://cdn.playwright.dev`. A hand-built `/dbazure/...` URL returned **400** to a plain curl
  while `https://cdn.playwright.dev/builds/cft/<ver>/win64/chrome-win64.zip` returned **206**. Copy the
  form from `playwright-core/lib/coreBundle.js` (`PLAYWRIGHT_CDN_MIRRORS`) instead of reconstructing it.
- The npm-China mirror's `playwright` tree is **not usable on Windows**: `builds/cft/<ver>/` has only
  `linux64/` and `mac-arm64/`, and `builds/chromium/<revision>/` only carries `chromium-linux-arm64.zip`.
  Setting `PLAYWRIGHT_DOWNLOAD_HOST` there 404s.
- The same host's `chrome-for-testing` tree **has** the files:
  `/-/binary/chrome-for-testing/<browserVersion>/win64/` → `chrome-win64.zip`,
  `chrome-headless-shell-win64.zip`, `chromedriver-win64.zip`. Use this artifact-keyed mirror for manual
  pre-seeding.
- Measured from a China consumer line with no proxy: official CDN ≈ 63 KB/s (192 MB ≈ 50 min) vs the
  npm-China mirror ≈ 3.2 MB/s (≈ 1 min). That 50-minute path is what a 10-minute install cap kills.

## Pre-seed recipe

```powershell
$ver  = "151.0.7922.34"           # = browserVersion from browsers.json
$rev  = "1234"                    # = revision from browsers.json
$base = "https://registry.npmmirror.com/-/binary/chrome-for-testing/$ver/win64"
$dst  = "$env:LOCALAPPDATA\ms-playwright"

New-Item -ItemType Directory -Force "$dst\chromium-$rev" | Out-Null
curl.exe -L -o "$env:TEMP\chrome-win64.zip" "$base/chrome-win64.zip"
Expand-Archive "$env:TEMP\chrome-win64.zip" -DestinationPath "$dst\chromium-$rev" -Force
New-Item -ItemType File -Force "$dst\chromium-$rev\INSTALLATION_COMPLETE" | Out-Null

New-Item -ItemType Directory -Force "$dst\chromium_headless_shell-$rev" | Out-Null
curl.exe -L -o "$env:TEMP\chs.zip" "$base/chrome-headless-shell-win64.zip"
Expand-Archive "$env:TEMP\chs.zip" -DestinationPath "$dst\chromium_headless_shell-$rev" -Force
New-Item -ItemType File -Force "$dst\chromium_headless_shell-$rev\INSTALLATION_COMPLETE" | Out-Null
```

Verified: URLs, sizes, throughput, and that the zips' own top-level folder is `chrome-win64/` / 
`chrome-headless-shell-win64/` (read from the zip central directory with a range request, not assumed).
The directory layout and the `INSTALLATION_COMPLETE` / `DEPENDENCIES_VALIDATED` markers mirror a working
install on disk. If Playwright re-downloads anyway, fall back instead of fighting it: proxy plus a raised
caller download ceiling, or skip browsers with a browser-executable override.

## Env vars

- `PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST` — per-browser host, takes precedence over
  `PLAYWRIGHT_DOWNLOAD_HOST`. The full path (`builds/cft/...`) is appended to whatever you set, so a
  mirror must reproduce that layout.
- `PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT` — milliseconds; raise it on a slow link, since a connect
  timeout reads as a dead mirror.
- `PLAYWRIGHT_BROWSERS_PATH` — relocate the install tree (keeps it off a small system drive).
- `HTTP_PROXY` / `HTTPS_PROXY` — honored by the downloader.

## Check whether it landed

```powershell
ls "$env:LOCALAPPDATA\ms-playwright"      # expect chromium-<rev> and chromium_headless_shell-<rev>
cd "<project>"; npx playwright install chromium   # should report already installed
```

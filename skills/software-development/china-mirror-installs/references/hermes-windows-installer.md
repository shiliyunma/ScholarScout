# Hermes on Windows (native): installer internals and where it stalls

Install/network depth only — features, config and surfaces belong to the bundled `hermes-agent` skill
and the docs site.

## What Hermes-Setup.exe is

A ~8 MB Tauri (Rust) GUI over WebView2 — a thin shell that installs nothing itself. It downloads
`scripts/install.ps1`, then drives it as child PowerShell processes: `install.ps1 -Manifest` for the
stage list, then `install.ps1 -Stage <name>` per stage. Its manifest declares that it does **not** request
UAC elevation and provisions only into `%LOCALAPPDATA%\hermes` (no HKLM, no Program Files).
Python/Node/Git/ripgrep/ffmpeg are installed *by* it, so "the machine has no dev software" is not the
prerequisite problem.

A CLI-only install (`iex (irm https://hermes-agent.nousresearch.com/install.ps1)`) needs no WebView2 —
use it when the GUI will not start. Note the GUI fetches `install.ps1` from raw.githubusercontent.com
while the CLI one-liner fetches it from the product domain, so they fail differently on a blocked network.

## Prerequisites that actually fail first

1. **Microsoft Edge WebView2 Runtime.** Missing → no window / "Could not find the WebView2 Runtime.
   Make sure it is installed or download it from https://developer.microsoft.com/en-us/microsoft-edge/webview2".
   Detect `pv` under
   `HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}`
   **and** its `HKCU` twin (newer installs land per-user; `pv` of `0.0.0.0` or empty means absent).
   Fix: `winget install -e --id Microsoft.EdgeWebView2Runtime`.
2. **VC++ 2015-2022 x64 redistributable** — missing produces a silent no-launch or `0xc000007b`, not an
   error dialog, so check it when "double-clicking does nothing".
3. **PowerShell reachable** at `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe` (falls back to
   `pwsh.exe`). Group policy blocking Windows PowerShell fails the install.
4. **Network to fetch install.ps1 and everything after it** — see the mirror rules in the parent SKILL.md.
5. **`%LOCALAPPDATA%\hermes` writable** (AV "protected folder access" blocks it) and a correct system
   clock (TLS).

Diagnostics, in order: `%LOCALAPPDATA%\hermes\logs\bootstrap.log`, then `logs\agent.log`.
The exe is PE32+ x86-64 and code-signed; a SmartScreen/AV block or a quarantined updater file shows up as
a *missing* file rather than a message.

## Stage manifest (`$InstallStages` in scripts/install.ps1)

This list **is** the GUI's step list — quote stages by their own title, never by your own numbering:

1 uv · 2 git · 3 node (detect) · 4 ripgrep + ffmpeg · 5 clone repository · 6 Python · 7 venv ·
8 **Python dependencies** · 9 **Node.js dependencies** (contains the browser payload) ·
10 build desktop app (only with `-IncludeDesktop`, i.e. the GUI installer) · 11 PATH · 12 config templates
· 13 messaging platform SDKs · 14 install marker · 15 configure · 16 gateway

- **Stage 8** runs `uv sync --extra all --locked` (hash-verified against `uv.lock`), falling back to
  `uv pip install -e ".[all]"`. Heaviest stage on a cold machine (~35 core packages plus
  google/fastapi/mcp/homeassistant/acp/youtube extras). uv ignores pip.conf — set `UV_DEFAULT_INDEX`.
- **Stage 9** is where the Playwright Chromium download bites: `npx --yes playwright install chromium`
  under a **600 s wall-clock cap** (`$nodeDepsTimeoutSec`; override with `NODE_DEPS_TIMEOUT`). Over the cap
  it is killed and the `browser_*` tools are silently absent afterwards. If `AGENT_BROWSER_EXECUTABLE_PATH`
  is set to an existing browser before launch, the installer writes it into `.env` and skips the Chromium
  download ("Explicit browser override set -- Chromium download will be skipped"). Windows `install.ps1`
  has **no `-SkipBrowser`** switch (only `install.sh` has `--skip-browser`); the env override is the way to
  skip. Electron for the stage-10 desktop build has a built-in npmmirror fallback (`ELECTRON_MIRROR`
  overrides it).
- **Stage 13** bootstraps **pip** (`python -m ensurepip`) — not uv — and only for platforms whose token is
  already in `.env` (`TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN`,
  `WHATSAPP_ENABLED`). An empty `.env` makes it a no-op, which rules it out as a slow-step culprit; when it
  does run, configure pip's index, not uv's.

Flags (`install.ps1` param block): `-NoVenv -SkipSetup -SkipComputerUse -Branch -Commit -Tag -InstallDir
-HermesHome -IncludeDesktop -Manifest -NonInteractive -ForceCommit`.

Re-running an interrupted install is safe and progressive: completed stages self-detect and skip, and uv's
wheel cache survives, so advice to "start over" is usually wrong.

## Inspecting it yourself instead of guessing

```bash
grep -n "param(" -A 40 install.ps1                     # real flags for this platform
grep -n 'Name = "' install.ps1                       # stage manifest: name, Title, category
grep -n "function Install-<X>" -A 60 install.ps1      # what one stage downloads
```

For the GUI exe, extract ASCII + UTF-16 strings in Python and grep for error text, CDN hosts and runtime
names — that is how the WebView2 requirement, the raw.githubusercontent fetch and the stage protocol were
pinned down. Stage titles are *not* embedded in the exe; they come from the manifest at runtime, which is
why numbering must come from the script.

`hermes doctor` is the post-install check; `hermes update` re-runs dependency sync and a desktop rebuild
without wiping `HERMES_HOME`.

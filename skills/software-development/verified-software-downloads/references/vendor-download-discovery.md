# Where official asset URLs live

General shape: the **docs** page tells you what to install, the **product/marketing site** holds the
actual binary, and the binary's URL usually carries a build/hash query that pins the exact build.

## Search order

1. **Docs page** — scan for "download"/"installer"/"recommended". If it links the site homepage
   rather than a file, follow it; that is normal, not a dead end.
2. **Homepage HTML hrefs** — the download button's target. Look for an asset host, e.g.
   `hermes-assets.nousresearch.com`, `download.<vendor>.com`, `releases.<vendor>.com`.
3. **GitHub releases API** when the vendor ships there:
   `curl -sL https://api.github.com/repos/<org>/<repo>/releases/latest` → `.assets[].browser_download_url`.
4. **JS-injected links** (SPA, href added at runtime): drive the page with the browser tool and read
   the link out of the live DOM instead of guessing a filename.

Trust rule: a link on the vendor's official page may point at an asset subdomain (CDN). Accept it when
the domain is clearly the vendor's own; never substitute a third-party mirror.

## Worked example — Hermes desktop installer

The docs installation page says "download the Hermes Desktop installer from our website" and links the
homepage; the homepage hrefs carry the real assets:

- Windows: `https://hermes-assets.nousresearch.com/Hermes-Setup.exe?build=<hash>`
- macOS: `https://hermes-assets.nousresearch.com/Hermes-Setup.dmg?build=<hash>` (Apple Silicon only)

The Windows file is a ~8 MB **online stub** (electron-winstaller style), Authenticode-signed by
`Nous Research Inc.` — say that explicitly so nobody plans an offline install. A git-based install on
Windows lives at `%LOCALAPPDATA%\hermes\hermes-agent` with launchers in `%LOCALAPPDATA%\hermes\bin`,
so the desktop installer is for other machines or for recovering a broken local environment, not a step
the user must run on a machine that already works.

If the user needs a genuinely offline path, the sources are the vendor's shell/PowerShell installer
script on the same site (command-line install) and the source archive — offer those rather than
pretending the stub installer works without network.

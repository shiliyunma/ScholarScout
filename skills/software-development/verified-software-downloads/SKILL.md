---
name: verified-software-downloads
description: "Use when fetching a vendor installer or release binary."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [download, installer, verification, checksum, signature, windows, release-artifacts]
---

# Verified Software Downloads

Deliverable: the artifact is on disk at the path the user named, **and** you can prove it is the vendor's real build (right size, recorded hash, valid signature) — not merely that `curl` exited 0.

## When to use

- "find <product>'s installer and put it in <dir>", "download <release> for me", "give me the setup file".
- Any task whose output is a file the user will run themselves or hand to someone else.

Not for: researching source code (use a repo skill). If the vendor already publishes an install script, name it instead of inventing a flow. Installing on **this** machine normally goes through the package manager or the existing repo checkout — but when the ask is *"update the app already installed here to the latest version"*, first check whether the app's own updater has already staged the new build, before downloading anything (see Pitfalls).

## Procedure

1. **Say which artifact you are fetching before you fetch it.** The candidates: the vendor's own installer, an offline/portable bundle or source archive, or a copy of the tree already installed locally. If the product is installed on this machine, give its install path and method in the reply — the vendor installer is then for another machine or for recovery, not a needed reinstall. Don't block on a clarifying question for a low-stakes choice: act on the most likely reading and offer the other candidate in the same message.

2. **Take the canonical URL, never a reconstructed one.** Vendor docs frequently link the *product site* rather than the file. Find the page, save it, then pull the asset hrefs out of the HTML:
   ```
   curl -sL --max-time 60 "https://vendor.example/docs/install" -o "$LOCALAPPDATA/Temp/page.html"
   grep -oE 'https?://[^"]+\.(exe|dmg|msi|pkg|zip|tar\.gz|AppImage|deb)' page.html | sort -u
   ```
   If the pattern starts needing nested quoting, parse it in `execute_code` with Python instead of fighting the shell (see Pitfalls). Copy the href **verbatim**, build/version query included. See `references/vendor-download-discovery.md` for where asset URLs hide per vendor type.

3. **Probe the headers before downloading** — confirms 200, the real byte count, and the artifact's nature:
   ```
   curl -sIL --max-time 60 "<url>" | grep -iE "HTTP/|content-length|content-type|last-modified"
   ```
   A 5-15 MB "Setup.exe" for a desktop app is usually an **online stub** that pulls the payload at install time. Say so, or the user plans an offline install it cannot do.

4. **Download straight into the destination the user named.** Quote the path — it may be non-ASCII or contain spaces (git-bash accepts `D:/dir with space/`):
   ```
   cd "D:/目标目录" && curl -L --max-time 300 -o "Product-Setup.exe" "<url>" \
     -w "http_code=%{http_code} size=%{size_download} speed=%{speed_download}\n"
   ```
   On a slow network let it run with a generous timeout (foreground max 600s); do not switch to an unverified mirror just for speed — a different mirror can serve a different build.

5. **Verify against step 3's probe.** Use `scripts/verify_artifact.sh <file> [expected_sha256]`, or by hand:
   ```
   ls -la "D:/目标目录/Product-Setup.exe"; sha256sum "D:/目标目录/Product-Setup.exe"
   ```
   For executables also check the code signature (Windows, note the `\$` escaping needed in git-bash):
   ```
   powershell -NoProfile -Command "\$f='D:\目标目录\Product-Setup.exe'; \$s=Get-AuthenticodeSignature \$f; Write-Output ('Status: ' + \$s.Status); Write-Output ('Signer: ' + \$s.SignerCertificate.Subject)"
   ```
   Gate before you call it done: byte count == Content-Length, and Status `Valid` with a signer matching the vendor. Wrong size or unsigned = a failed task; report it, don't hand the file over. Details and status meanings: `references/windows-artifact-verification.md`.

6. **Report in this shape:** absolute path, byte size, SHA256, signature status + signer, what it can and cannot do offline, and one offered follow-up (offline/source bundle, a `校验.txt` beside it). Then stop — don't pre-emptively download extra variants the user didn't ask for.

## Pitfalls

- Concluding "there is no download" from a docs page. The download button's href lives on the product/marketing homepage; scrape the homepage's hrefs before declaring the artifact unavailable.
- Dropping a version/build query from the URL, or re-typing it from memory. The `?build=<hash>` style parameter is what pins the build; copy the href verbatim.
- Treating `curl` exit 0 as success. Interrupted transfers and HTML error pages save fine — always compare size/hash and check the signature.
- Stacking escaped quotes in a long inline shell one-liner (HTML/JSON parsing, multi-command greps) gets the command rejected by the parser or silently mangles quoting. Parse in `execute_code` with Python, or write a small script file and run it.
- `grep -r` across a repo checkout containing `node_modules` (or the Hermes source tree) runs for minutes. Use ripgrep (`search_files`) with file globs.
- Handing over a file whose provenance you haven't established. The vendor's own asset host or its linked CDN is fine; a random mirror is not.
- Choosing a mirror — or a `*_DOWNLOAD_HOST`-style override — because it answers 200. A mirror directory can exist while the file you need is missing for your platform: mirrors sync per-OS and per-path-layout, and the consuming tool may have changed layout since the mirror was populated. List the mirror's JSON inventory and grep for your exact filename first; when the mirror is keyed by the *consumer* rather than the *artifact*, check that the consumer's current path scheme exists there at all. Package-ecosystem mirrors (PyPI, npm, Electron, Chrome-for-Testing) are legitimate for dependencies; substituting one for a vendor's own installer is not.
- Quoting the raw `curl` progress bar back to the user. Report the destination, size, hash and what the file is — the transfer log is noise.
- **Before downloading to "update app X", check whether X's own updater already staged the new build.**
  Modern apps download, unpack and stage the next version into a commit/version-named subfolder beside
  the running one, then finish the swap on the next launch — so a manual installer download is wasted
  work, and the installer will usually refuse to run anyway. Evidence to gather in order: (1) a second
  version directory beside the running one — read its `package.json` / version resource to confirm
  which version is staged; (2) a still-running `*Setup*` process under `%TEMP%` blocked waiting for the
  app to exit; (3) the app's own reported version lagging what is staged. When a vendor installer does
  exit non-zero within seconds, get the real reason from its log (`/LOG="<path>"` on Inno-style setups)
  instead of guessing — it names the blocker outright.
- **Never force-kill a running app to let an update land.** An editor/IDE can hold unsaved work, and
  `taskkill /F` bypasses the app's own crash/hot-exit recovery, so the "fix" can destroy the user's
  data. Stop at reporting the state and ask which they want (they close it themselves / authorise a
  graceful close / hold off). A graceful window close (`CloseMainWindow()`) preserves recovery; a
  forced kill does not.

## Files

- `scripts/verify_artifact.sh` — one-shot bytes + sha256 + code-signature check (bash/git-bash).
- `references/vendor-download-discovery.md` — where official asset URLs live, and a worked example.
- `references/windows-artifact-verification.md` — verification commands; what each Authenticode status means.

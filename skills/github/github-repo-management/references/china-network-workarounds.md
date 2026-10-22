# China Network Workarounds for GitHub Access

When GitHub is blocked or unreliable (common in China), standard `git clone` and `curl` to `github.com` frequently fail with timeouts or connection resets. Below are reliable alternatives proven in practice.

## Problem Diagnosis

```bash
# Git clone: usually hangs for ~21s then fails
git clone https://github.com/owner/repo.git
# → fatal: unable to access... Failed to connect to github.com port 443 after 21109 ms

# curl to github.com: may succeed or fail depending on DNS/CDN routing
curl -s --connect-timeout 10 -o /dev/null -w "%{http_code}" https://github.com
# → 200 (sometimes), 000 (other times)

# But browser can reach GitHub — the Hermes desktop app's built-in browser
# uses a different network stack that may bypass restrictions.
```

## Reliable Workarounds

### 1. `codeload.github.com` (most reliable — ~95% success rate on first try, ~100% on retry)

GitHub's code download CDN (`codeload.github.com`) uses different infrastructure than `github.com` and `raw.githubusercontent.com`, and is often accessible when the main domain is not.

```bash
# Download as ZIP instead of cloning
curl -L --connect-timeout 60 --max-time 300 \
  -o "repo-name.zip" \
  "https://codeload.github.com/owner/repo/zip/refs/heads/master"

# Or for repos using 'main' as default branch
curl -L --connect-timeout 60 --max-time 300 \
  -o "repo-name.zip" \
  "https://codeload.github.com/owner/repo/zip/refs/heads/main"
```

**Important**: Check the default branch first — use the GitHub web UI to verify `master` vs `main`. Wrong branch gives 14-byte empty ZIP.

**Retry behavior**: The first download attempt may time out after 300s with partial data (corrupted ZIP). Always retry — the second attempt typically completes in seconds at 4-8 MB/s. This is the most important gotcha to remember: a single failure does NOT mean the mirror is down.

### 2. Browser-based download

The Hermes desktop GUI's built-in browser can often reach GitHub when the terminal can't. Navigate to the GitHub repo page, click "Code" → "Download ZIP".

This bypasses terminal-level network restrictions because the browser uses a different network stack/proxy configuration.

### 3. Using the browser for raw file content

When full clone/zip is needed but network is too slow, use the browser to access individual files at:
```
https://raw.githubusercontent.com/owner/repo/branch/path/to/file
```

### 4. git clone with workarounds (less reliable)

These occasionally work but are not recommended as primary approach:

```bash
# Force IPv4 (sometimes IPv6 is the problem)
git -c http.version=HTTP/1.1 clone --depth 1 https://github.com/owner/repo.git

# Increase timeouts
GIT_HTTP_MAX_RETRIES=5 git -c http.lowSpeedLimit=0 -c http.lowSpeedTime=999999 \
  clone --depth 1 https://github.com/owner/repo.git
```

### 5. GitHub mirror sites (unreliable — often downtimed)

These mirrors have high churn and frequently change or get blocked:
- `hub.fastgit.xyz` — often down
- `github.com.cnpmjs.org` — DNS resolution failure
- `ghproxy.com` / `gh-proxy.com` — connection resets
- `gitclone.com` — returns empty repos

If a mirror works in your session, use it — but never hardcode a mirror into a skill or reference.

## Known Patterns

| Access method | Typical behavior in China |
|---|---|
| `git clone https://github.com/...` | ❌ Usually fails (port 443 blocked) |
| `curl https://github.com` | ⚠️ Inconsistent — sometimes works |
| `curl https://codeload.github.com/...` | ✅ Most reliable — fast on first try (~8MB/s) or instant on retry |
| Browser (Hermes Desktop) | ✅ Works (different network stack) |
| `raw.githubusercontent.com` | ⚠️ Inconsistent |
| `api.github.com` | ⚠️ Inconsistent |
| GitHub mirrors | ❌ High churn, often broken |

## Example: Full Download Workflow

```bash
# 1. Identify the default branch via browser (check if button shows "master" or "main")
# 2. Download as ZIP (retry if first attempt times out)
BRANCH="master"  # or "main" — verify first
curl -L --connect-timeout 60 --max-time 300 \
  -o "project.zip" \
  "https://codeload.github.com/owner/repo/zip/refs/heads/$BRANCH"

# 2b. Verify ZIP integrity — don't trust exit code alone (timeout exits non-zero with partial data)
if [ ! -f "project.zip" ] || ! unzip -tq "project.zip" &>/dev/null; then
  echo "Retrying download (corrupted or empty)..."
  curl -L --connect-timeout 60 --max-time 300 \
    -o "project.zip" \
    "https://codeload.github.com/owner/repo/zip/refs/heads/$BRANCH"
fi

# 2c. Branch fallback: if main returns empty (14 bytes), try master
if [ -f "project.zip" ] && [ "$(stat -c%s project.zip 2>/dev/null || stat -f%z project.zip 2>/dev/null)" -lt 100 ]; then
  echo "Got tiny ZIP ($(wc -c < project.zip) bytes) — trying alternate branch..."
  BRANCH=$([ "$BRANCH" = "main" ] && echo "master" || echo "main")
  curl -L --connect-timeout 60 --max-time 300 \
    -o "project.zip" \
    "https://codeload.github.com/owner/repo/zip/refs/heads/$BRANCH"
fi

# 3. Verify again, then extract
unzip -tq "project.zip" || { echo "ZIP still corrupted"; exit 1; }
mkdir project-dir && unzip -qo project.zip -d project-dir
first_subdir=$(ls project-dir | head -1)
if [ -n "$first_subdir" ] && [ -d "project-dir/$first_subdir" ]; then
  mv "project-dir/$first_subdir"/* "project-dir/" 2>/dev/null
  mv "project-dir/$first_subdir"/.* "project-dir/" 2>/dev/null
  rm -rf "project-dir/$first_subdir"
fi

# 4. Clean up
rm -f project.zip
```

## Batch Download Pattern (Parallel)

When collecting from multiple repos simultaneously:

```bash
# Use terminal(background=true) for each curl call
# so they run in parallel without blocking the agent
for repo in "owner1/repo1" "owner2/repo2"; do
  owner="${repo%%/*}"
  name="${repo##*/}"
  curl -L --connect-timeout 60 --max-time 300 \
    -o "${name}.zip" \
    "https://codeload.github.com/${repo}/zip/refs/heads/master" &
done
wait
# Then verify each
for f in *.zip; do
  unzip -tq "$f" && echo "$f OK" || echo "$f CORRUPTED — retry needed"
done
```

## Why the First Attempt Often Fails

In practice, the first `curl` to `codeload.github.com` frequently stalls around 50-80% and times out at 300s with a corrupted ZIP. The **second attempt** almost always completes in <10 seconds at 4-8 MB/s. This is a characteristic of GitHub's CDN routing from China — the first connection establishes a slow path, and the cached route serves retries much faster. **Always retry at least once** before falling back to alternatives.
```

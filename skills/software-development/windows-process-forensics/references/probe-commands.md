# Probe catalogue

All commands assume git-bash (`terminal` tool) on Windows with `<u>` = `$USERNAME`.

## Process present / which process owns a connection

```bash
# plain tasklist, then grep (never use //FI under MSYS)
tasklist | grep -i -E "name1|name2"

# connections joined to process NAME (not just PID)
powershell -NoProfile -Command "Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue | Where-Object {\$_.RemoteAddress -notmatch '^(127\.|::1)'} | Select-Object -First 40 RemoteAddress,RemotePort,OwningProcess,@{n='Proc';e={(Get-Process -Id \$_.OwningProcess -ErrorAction SilentlyContinue).ProcessName}} | Format-Table -AutoSize"

# listening ports when hunting a P2P/peer port
netstat -ano | grep -i LISTENING
```

## Locate the binary

```bash
# shallow sweep of the vendor-bearing roots
for d in "C:/Program Files" "C:/Program Files (x86)" "C:/ProgramData" \
         "C:/Users/<u>/AppData/Roaming" "C:/Users/<u>/AppData/Local" "C:/" "D:/" "E:/"; do
  echo "--- $d"; ls "$d" | grep -i -E "vendor|keyword"; done

# scoped recursion (fast, native, no MSYS path problems)
cd "C:/Program Files (x86)/<Vendor>" && cmd /c "dir /s /b *Pattern*"

# execution history
ls "C:/Windows/Prefetch" | grep -i <name>
```

Anti-pattern that costs a whole tool call: `Get-ChildItem -Recurse` over `C:\` `D:\` `E:\`, or
`search_files(target='files', path='C:\')`. Both exceed the timeout on a normal drive.

## Fingerprint

```bash
powershell -NoProfile -Command "\$f=Get-Item 'C:\Path\App.exe'; \$v=\$f.VersionInfo; 'FileVersion='+\$v.FileVersion; 'Product='+\$v.ProductName; 'Company='+\$v.CompanyName; 'Desc='+\$v.FileDescription; 'Size='+\$f.Length; 'Modified='+\$f.LastWriteTime; \$s=Get-AuthenticodeSignature \$f.FullName; 'SigStatus='+\$s.Status; 'Signer='+\$s.SignerCertificate.Subject"
```

Read: `CompanyName`, signer CN, `SigStatus`. `Valid` + a vendor CN ⇒ legitimate vendor software;
say so plainly. The sibling-file check is often what names the product:

```bash
cd "<install dir>" && cmd /c "dir /s /b *Tiny* *Proxy* *Setup*"
```

## Installed programs / product footprint

```bash
powershell -NoProfile -Command "Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*','HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue | Where-Object { \$_.DisplayName -match 'keyword|管家|安全' } | Select-Object DisplayName,DisplayVersion,InstallLocation | Format-Table -AutoSize | Out-String -Width 200"
```

Chinese `DisplayName` values come back mojibake through the pipe — match on them anyway (the bytes
survive), but read `InstallLocation` to identify the row.

## Persistence and firewall

```bash
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
reg query "HKLM\Software\Microsoft\Windows\CurrentVersion\Run"
reg query "HKLM\Software\Microsoft\Windows\CurrentVersion\Run\LenovoDisabled"   # rem| = DISABLED
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run\LenovoDisabled"

powershell -NoProfile -Command "Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { \$_.TaskName -match 'X' -or \$_.TaskPath -match 'X' } | Select-Object TaskName,TaskPath,State | Format-Table -AutoSize"

# which firewall rules touch this product, and what else is in it
powershell -NoProfile -Command "Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | Where-Object { \$_.Program -match 'X' } | ForEach-Object { \$r = \$_ | Get-NetFirewallRule; \"\$(\$r.DisplayName) | \$(\$r.Direction) | \$(\$r.Action) | \$(\$r.Enabled) | \$(\$_.Program)\" }"
```

## Activity timeline from disk

```bash
# newest files in a vendor cache dir first, then size only that dir
cd "<cache dir>" && ls -lt --time-style=long-iso | head -12
for d in "<cache1>" "<cache2>"; do printf "%s : " "$d"; du -sh "$d" 2>/dev/null | cut -f1; done

# distinct manifest groups = distinct download rounds
ls -1 *.manifest | sed 's/_[0-9]*\.manifest//' | sort -u

# files written in the last N days anywhere under a vendor tree
powershell -NoProfile -Command "Get-ChildItem 'C:\ProgramData\<Vendor>' -Recurse -Include '*.db','*.dat','*.log','*.json' -ErrorAction SilentlyContinue | Where-Object { \$_.LastWriteTime -gt (Get-Date).AddDays(-45) -and \$_.Length -gt 10000 } | Sort-Object LastWriteTime -Descending | Select-Object -First 25 FullName,Length,LastWriteTime | Format-Table -AutoSize | Out-String -Width 250"
```

This last one is fine because it is scoped to ONE vendor tree — do not widen it to a drive root.

## Failure output: is the vendor log even readable?

```bash
head -c 200 "<logfile>" | tr -d '\0' | head -3
```

If it is not text, stop and report that the byte counters are unreadable. Do not estimate.

## Search-snippet fallback (for 「这个进程是什么」)

百度贴吧 / 知乎 / 百度安全验证 walls answer a bot with a blank page or a challenge. The search
ENGINE's result snippets still carry the community answer. Bing serves plain HTML at
`https://cn.bing.com/search?q=<urlencoded>&setlang=zh-CN&count=20`; parse `<li class="b_algo">`
blocks for title / href / body text.

Write it to a `.py` file in `$LOCALAPPDATA/Temp` and run it — an inline `curl | python -c` with
nested quotes gets blocked or mangled:

```python
import re, html, urllib.request, urllib.parse

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", "ignore")

def strip(t):
    t = re.sub(r"<script.*?</script>", " ", t, flags=re.S)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()

def bing(q, n=8):
    h = fetch("https://cn.bing.com/search?q=%s&setlang=zh-CN&count=20" % urllib.parse.quote(q))
    out = []
    for m in re.finditer(r'<li class="b_algo".*?</li>', h, re.S):
        s = m.group(0)
        u = re.search(r'href="(http[^"]+)"', s)
        out.append({"url": u.group(1) if u else "", "body": strip(s)[:450]})
    return out[:n]

for q in ["<process name> 是什么 占用 流量", "<process name> 关闭 禁止 方法"]:
    print("=" * 20, q)
    for r in bing(q):
        print("U:", r["url"]); print("B:", r["body"]); print("-")
```

Guards:
- A brand-name query gets hijacked by the vendor's own marketing pages — expect it, and prefer
  queries carrying the symptom (偷跑流量 / 占用后台 / 关闭方法).
- A forum snippet is a hypothesis, never a finding. The signature and version from the fingerprint
  step are the verification, and they outrank every forum claim.

---
name: windows-process-forensics
description: Use when an unknown Windows process or exe misbehaves.
---

# Windows 进程 / 资源占用排查

Triggers: 「这个程序是什么」, 「它用了我 200G 流量」, a strange name in 任务管理器, an app eating
CPU/磁盘/RAM, "am I infected". Answer from evidence on the machine — the exe on disk carries the
vendor, the version and the signature. Read it before you say anything.

## Order of work

1. Locate — is it running, and where does the binary live
2. Fingerprint the binary ← the answer-producing step
3. Attribute the resource (network / disk / CPU) with timestamps and sizes
4. Check persistence
5. Mitigate with commands the user runs

Do not jump to mitigation. A fix proposed before the binary is fingerprinted is a guess, and the
user's real question is almost always "am I infected".

### 1. Locate

Shallow first, then scope the recursion. A handful of `ls` calls beat one whole-drive search:

```bash
for d in "C:/Program Files" "C:/Program Files (x86)" "C:/ProgramData" \
         "C:/Users/<u>/AppData/Roaming" "C:/Users/<u>/AppData/Local" "C:/" "D:/" "E:/"; do
  echo "--- $d"; ls "$d" | grep -i -E "keyword1|keyword2"; done
```

Then recurse only inside what matched:

```bash
cd "C:/Program Files (x86)/<Vendor>" && cmd /c "dir /s /b *Pattern*"
```

- NEVER recurse a whole drive. PowerShell `Get-ChildItem -Path 'C:\','D:\','E:\' -Recurse` and
  `search_files(target='files', path='C:\')` both run for minutes and die on the tool timeout —
  you lose the call and learn nothing. Whole-drive traversal is worth it only after a shallow
  probe has already named the vendor.
- Vendor dirs under `AppData\Roaming\<Vendor>`, `AppData\Local\<Vendor>`, `ProgramData\<Vendor>`
  are the fastest way to see what a program actually did on this machine.

### 2. Fingerprint

```bash
powershell -NoProfile -Command "\$f=Get-Item 'C:\Path\App.exe'; \$v=\$f.VersionInfo; 'FileVersion='+\$v.FileVersion; 'Product='+\$v.ProductName; 'Company='+\$v.CompanyName; 'Desc='+\$v.FileDescription; 'Size='+\$f.Length; 'Modified='+\$f.LastWriteTime; \$s=Get-AuthenticodeSignature \$f.FullName; 'SigStatus='+\$s.Status; 'Signer='+\$s.SignerCertificate.Subject"
```

- `SigStatus=Valid` from a known vendor (read `CompanyName` and the signer CN) means legitimate
  vendor software: say that plainly in the FIRST line and name the parent product. Hedging here
  wastes the user's attention — they asked "what is it", not "maybe it's fine".
- Sibling files in the same directory (`*Proxy.dll`, `*Ex\`, `*Setup*`, a matching 32-bit twin)
  name the parent product when the binary name alone is ambiguous.
- Popular names collide across unrelated projects (a game download engine and a deep-learning
  framework can both be called `TinyDL`). Disambiguate by path + signer and state which one the
  user actually has.

### 3. Attribute the resource

**Live network** — join connections to process NAMES, which bare `netstat -ano` will not do:

```bash
powershell -NoProfile -Command "Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue | Where-Object {\$_.RemoteAddress -notmatch '^(127\.|::1)'} | Select-Object -First 40 RemoteAddress,RemotePort,OwningProcess,@{n='Proc';e={(Get-Process -Id \$_.OwningProcess -ErrorAction SilentlyContinue).ProcessName}} | Format-Table -AutoSize"
```

**30-day per-app totals** have no CLI: they live in the SRUM database
(`C:\Windows\System32\sru\SRUDB.dat`), which needs an ESE parser — do not burn the session on it.
Point the user at `resmon` → 网络 tab (live 发送/接收 per process; **发送 ≈ 接收 or larger means the
流量 is P2P seeding, not downloading** — that reframes "it downloaded 200G" into "it uploaded 200G")
and 设置 → 网络和 Internet → 数据使用量.

**Disk / cache timeline** — the readable proxy for "how often did it run". Vendor caches sit in a
`*cache*` dir beside the exe or beside each item it manages:

- `ls -lt --time-style=long-iso` on the cache dir first; repeated manifest/chunk sets at different
  mtimes = repeated download rounds. That is real evidence of recurring activity.
- `du -sh` ONLY the cache dirs. Never `du -sh` a whole game/media/library tree — it is minutes-long
  and will time out.
- Vendor logs are frequently encrypted or binary. Check the first bytes; if it is not text, say you
  cannot read the byte counters instead of estimating them.
- `ls C:/Windows/Prefetch | grep -i <name>` shows execution history of the binary.

### 4. Check persistence

- Autostart: `reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run"` and the HKLM twin.
  Also check the OEM `...\Run\LenovoDisabled` subkey: entries prefixed `rem|` are **disabled**
  startups — reporting those as active autostarts is the classic inverse error.
- Scheduled tasks: `Get-ScheduledTask | Where-Object { $_.TaskName -match 'X' -or $_.TaskPath -match 'X' }`.
- Firewall: `Get-NetFirewallApplicationFilter | Where-Object { $_.Program -match 'X' } | ForEach-Object { $r = $_ | Get-NetFirewallRule; "$($r.DisplayName) | $($r.Direction) | $($r.Action) | $($r.Enabled) | $($_.Program)" }`
  — also the cheapest way to discover *other* components of the same product.

### 5. Mitigate

Cheapest → heaviest, and hand the user the commands rather than executing system changes:

1. Kill the process (say what it interrupts — usually a download, not the app's core function).
2. The app's OWN setting first — upload/P2P limit, auto-update, preload, background residency.
   This is the fix that does not break the app, so it goes before any firewall block.
3. Outbound firewall block on the specific exe, with the delete command stated alongside and the
   tradeoff named (it also blocks that app's updates).
4. Leave it stopped (verify it is NOT in autostart) or uninstall.

Never write firewall rules or registry keys silently — firewall/registry edits are the user's call.
End by asking whether to run them.

## Pitfalls (git-bash / MSYS on Windows)

- **`tasklist //FI "IMAGENAME eq X"` cannot be trusted in either direction.** MSYS rewrites the `/FI`
  switch and the failure mode is inconsistent: sometimes an invalid-argument error, but it has also
  returned **an empty list with exit 0**. The empty case is the dangerous one — it reads as a
  confident "the process is not running", and every decision built on it (safe to replace files,
  safe to reinstall, the app is not in use) is silently inverted. Never conclude "not running" from a
  filtered `tasklist`: run plain `tasklist` and grep it, or use PowerShell
  `Get-Process | Where-Object {$_.ProcessName -like '*X*'}`. Confirm with a second, independent signal
  (`MainWindowTitle`, the app's own versioned install dirs, a lock/handle) before acting on the answer.
- Do not write `2>nul` inside a bash command — bash creates a file literally named `nul`. Use `2>/dev/null`.
- Give native tools native forward-slash paths (`C:/...`), never `/c/...`.
- Chinese text piped out of PowerShell returns mojibake (`WeGame••••`). Judge by ASCII fields (paths,
  version, signature) and re-query with `| Out-String -Width 250` when a display name matters.
- Inline nested quoting (curl piped to `python -c`, `reg query /f` with a pattern, redirections) is
  what gets commands blocked or mangled. Write a `.ps1` / `.py` file into `$LOCALAPPDATA/Temp` and
  run that instead — it works first try where inline forms do not.
- For a probe that may exceed the foreground timeout, launch with `background=True, notify=True`
  writing to a file, then read the file back. Do not hold a foreground call on a whole-drive scan.
- Community knowledge for 「某进程是什么」 lives mostly on 百度贴吧/知乎, which answer a bot with 安全验证
  or a blank page. Read search-engine result snippets (Bing's `b_algo` blocks) instead of fetching the
  page — and let the signature from step 2 outrank any forum claim.

## Deliverable

- Save a markdown report to `C:\Users\<u>\Desktop\<Topic>_排查报告.md`; the user prefers a file path
  plus a short chat answer over a wall of chat output.
- Report shape: 结论 → 本机证据表 → 先花一分钟确认是不是它 → 处置步骤 → 一句话总结.
- 结论 first and blunt: product name, signed-or-not, then the mechanism behind the resource use.
- Separate PROVEN from UNREADABLE. If logs were encrypted or a byte counter was unavailable, say the
  gap explicitly — never fill it with an estimate.
- Include a one-minute self-check the user can run to confirm the diagnosis themselves (e.g. resmon
  网络 tab showing 发送 > 接收).

## References

- `references/probe-commands.md` — copy-paste probe catalogue for steps 1–4, plus the search-snippet fallback.

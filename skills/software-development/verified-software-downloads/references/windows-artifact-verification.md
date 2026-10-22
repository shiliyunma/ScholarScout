# Verifying a downloaded artifact on Windows

Run these from git-bash unless a step says PowerShell.

## Size vs. what the server promised

```
curl -sIL "<url>" | grep -i content-length      # expected bytes
wc -c < "D:/path/File.exe"                      # bytes on disk
```

## Hash

```
sha256sum "D:/path/File.exe"
```

Compare with the vendor's published checksum when one exists. When none is published, record the
computed hash in the reply so the user can re-check it after copying the file to another machine.

## Code signature (authenticity, not just integrity)

```
powershell -NoProfile -Command "\$f='D:\path\File.exe'; \$s=Get-AuthenticodeSignature \$f; Write-Output ('Status: ' + \$s.Status); Write-Output ('Signer: ' + \$s.SignerCertificate.Subject)"
```

Inside the double-quoted `-Command` string, escape `$` as `\$` so bash does not expand it.

Status values:

- `Valid` — signed and the chain is trusted. Report the signer's `CN` and check it names the vendor.
- `NotSigned` — not a vendor-signed build. Wrong artifact or a repackaged copy; stop and re-download
  from the official host.
- `HashMismatch` — the file changed after signing. Treat as tampered: delete and re-download.
- `UnknownError` — typically an unavailable timestamp chain or offline revocation check. Do not declare
  it bad on this alone; fall back to the vendor-published checksum and signature thumbprint.

## Version metadata for the report

```
powershell -NoProfile -Command "(Get-Item 'D:\path\File.exe').VersionInfo | Select-Object FileVersion,ProductName,CompanyName | Format-List"
```

`FileVersion` is often a placeholder (e.g. `0.0.1`) even on a legitimate installer — never infer the
product version from it. Take the version from the vendor page or the build hash in the download URL.

## Also worth checking for installers

- Extension vs. real type: `file "D:/path/File.exe"` — a `.exe` that reports HTML/text means you saved
  an error page.
- If the download came from a release page, the notes usually state OS/arch limits (e.g. a macOS
  installer that is Apple-Silicon-only). Repeat that limit to the user: it is the most common reason the
  artifact "doesn't work" on their machine.

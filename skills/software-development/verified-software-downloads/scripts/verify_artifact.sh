#!/usr/bin/env bash
# Usage: verify_artifact.sh <file> [expected_sha256]
# Prints path, byte count, sha256, and for .exe/.dll/.msi the Authenticode status + signer.
# Exits non-zero when the hash does not match the expected value or the file is missing.
set -u

f=${1:-}
if [ -z "$f" ]; then
  echo "usage: verify_artifact.sh <file> [expected_sha256]" >&2
  exit 2
fi
if [ ! -f "$f" ]; then
  echo "not a file: $f" >&2
  exit 2
fi

echo "path:   $f"
echo "bytes:  $(wc -c < "$f" | tr -d ' ')"

sha=$(sha256sum "$f" | awk '{print $1}')
echo "sha256: $sha"

if [ -n "${2:-}" ]; then
  if [ "$sha" = "$2" ]; then
    echo "sha256: matches expected value"
  else
    echo "sha256: MISMATCH (expected $2)" >&2
    exit 1
  fi
fi

case "$f" in
  *.exe|*.dll|*.msi)
    if command -v powershell >/dev/null 2>&1; then
      win=$(cygpath -w "$f" 2>/dev/null || printf '%s' "$f")
      powershell -NoProfile -Command "\$s=Get-AuthenticodeSignature '$win'; Write-Output ('sig:    ' + \$s.Status); if (\$s.SignerCertificate) { Write-Output ('signer: ' + \$s.SignerCertificate.Subject) }"
    else
      echo "sig:    (powershell unavailable on this host)"
    fi
    ;;
  *)
    echo "sig:    (not a signed-binary type; skipping Authenticode check)"
    ;;
esac

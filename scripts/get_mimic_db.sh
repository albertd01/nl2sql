#!/usr/bin/env bash
# Download the MIMIC-IV demo database in EHRSQL 2024 format (the database used by the demo and
# the MIMIC evaluation suite) to data/mimic_iv.sqlite.
#
# Source: https://github.com/glee4810/ehrsql-2024 (data/mimic_iv/mimic_iv.sqlite), pinned to a commit
# and verified by checksum. It is built from the MIMIC-IV Clinical Database Demo 2.2 on PhysioNet
# (Open Data Commons Open Database License v1.0) by EHRSQL 2024's preprocess/preprocess.sh; running
# that script reproduces the same table contents.
set -euo pipefail

COMMIT="f9e1aa02160d39e3f8df52bf5c69c5cf2e472499"
SHA256="a26a60261d7e3bbf1869ffd92efe1803abdbce3eb256a2883fc0439c63427796"
URL="https://raw.githubusercontent.com/glee4810/ehrsql-2024/$COMMIT/data/mimic_iv/mimic_iv.sqlite"

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:-$PROJECT/data/mimic_iv.sqlite}"
mkdir -p "$(dirname "$TARGET")"

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

if [ -f "$TARGET" ] && [ "$(sha256 "$TARGET")" = "$SHA256" ]; then
  echo "Already present and verified: $TARGET"
  exit 0
fi

echo "Downloading MIMIC-IV demo (EHRSQL 2024 format, 37 MB)..."
tmp="$TARGET.part"
curl -fL --retry 3 -o "$tmp" "$URL"
actual="$(sha256 "$tmp")"
if [ "$actual" != "$SHA256" ]; then
  rm -f "$tmp"
  echo "Checksum mismatch (got $actual, expected $SHA256) — not using the download." >&2
  exit 1
fi
mv "$tmp" "$TARGET"
echo "Saved and verified: $TARGET"

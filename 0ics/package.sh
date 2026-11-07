#!/usr/bin/env bash
# 0ics/package.sh <out-dir> — release tarball of the LSAI fork (runtime files only)
# Output: <out-dir>/xcode-build-server-<VERSION>.tar.gz (top folder xcode-build-server/) and its .sha256
set -euo pipefail
[[ $# -eq 1 ]] || { echo "usage: $0 <out-dir>" >&2; exit 2; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VER="$(tr -d '[:space:]' < "$ROOT/0ics/VERSION")"
OUT="$(mkdir -p "$1" && cd "$1" && pwd)"
NAME="xcode-build-server-$VER.tar.gz"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/xcode-build-server/0ics"
cp -p "$ROOT"/{xcode-build-server,server.py,misc.py,compile_database.py,xcactivitylog.py,xclog_parser.py,LICENSE} "$STAGE/xcode-build-server/"
cp -Rp "$ROOT/config" "$ROOT/xcode" "$STAGE/xcode-build-server/"
cp -p "$ROOT/0ics/VERSION" "$STAGE/xcode-build-server/0ics/"
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} +
tar -C "$STAGE" -czf "$OUT/$NAME" xcode-build-server
(
    cd "$OUT"
    if command -v sha256sum >/dev/null; then sha256sum "$NAME"; else shasum -a 256 "$NAME"; fi
) > "$OUT/$NAME.sha256"
echo "$OUT/$NAME"

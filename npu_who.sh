#!/usr/bin/env bash
# Convenience wrapper for Ascend servers.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$DIR/npu_who.py" "$@"

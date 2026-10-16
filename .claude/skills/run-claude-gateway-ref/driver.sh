#!/usr/bin/env bash
# One foreground run owns startup, captures and cleanup. No persistent PIDs.
set -euo pipefail
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SKILL_DIR/driver.py" "$@"

#!/usr/bin/env bash
# Backward-compatible entrypoint. The deployer deliberately leaves privileged
# system-package and sudoers changes to the operator.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "setup.sh now delegates to the unprivileged deploy surface (scripts/deploy.py)." >&2
exec python3 "$ROOT/scripts/deploy.py" --source "$ROOT" "$@"

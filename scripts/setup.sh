#!/usr/bin/env bash
# Bootstrap ModelArk: Python venv + deps + system packages.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "→ Python venv + package (editable)"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e .

echo "→ System packages (needs sudo): git-annex, smartmontools"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get install -y git-annex smartmontools
else
  echo "  non-apt system — install git-annex + smartmontools with your package manager"
fi

echo
echo "✓ Setup complete."
echo "  Optional (gated repos / higher rate limits):  .venv/bin/hf auth login"
echo "  Launch the portal:                            .venv/bin/modelark serve"
echo "  Disk Health (SMART):  grant smartctl passwordless sudo (see README > Setup) — do NOT run the portal as root"

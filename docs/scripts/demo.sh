#!/usr/bin/env bash
# Regenerate docs/assets/demo.png from a real run of `uv run dly --help`.
# Requires freeze (github.com/charmbracelet/freeze) and a synced uv env.
set -euo pipefail
cd "$(dirname "$0")/../.."
freeze --execute "uv run dly --help" \
  --theme github-dark --background "#0d1117" --window --padding 24 --font.size 28 \
  --output docs/assets/demo.png

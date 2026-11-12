#!/usr/bin/env bash
# One-time setup on macOS (Apple Silicon). Safe to re-run.
#   ./scripts/setup_mac.sh            copy YouTube descriptions (default, no AI installed)
#   ./scripts/setup_mac.sh --with-ai  also install the local AI (only for description_source: ai)
set -euo pipefail
cd "$(dirname "$0")/.."
WITH_AI=0
[[ "${1:-}" == "--with-ai" ]] && WITH_AI=1

if ! command -v brew >/dev/null; then
  echo "Homebrew is required. Install it from https://brew.sh and re-run this script."; exit 1
fi

echo "==> Installing Python and ffmpeg (free)"
brew install python@3.12 ffmpeg

echo "==> Creating virtual environment"
PY="$(brew --prefix)/bin/python3.12"
"$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
if [[ $WITH_AI == 1 ]]; then
  .venv/bin/pip install -q -e '.[whisper]'
else
  .venv/bin/pip install -q -e .
fi

echo "==> Installing the browser used for Spotify uploads"
.venv/bin/playwright install chromium

if [[ $WITH_AI == 1 ]]; then
  echo "==> Installing Ollama and the local AI model (about 2 GB, one time)"
  brew install ollama
  brew services start ollama   # idle in the background; uses almost no RAM until a model loads
  sleep 3
  ollama pull qwen2.5:3b
fi

echo "==> Creating config"
.venv/bin/youtify init || true

mkdir -p "$HOME/.local/bin"
ln -sf "$PWD/.venv/bin/youtify" "$HOME/.local/bin/youtify"

cat <<MSG

Done. Next steps:
  1. Edit ~/Youtify/config.yaml (channels, video folders, Spotify show ids)
  2. youtify doctor
  3. youtify login          (log in to Spotify for Creators once)
  4. youtify sync && youtify scan && youtify prepare --limit 3
  5. Read the files in ~/Youtify/review/, then: youtify upload --limit 1
  6. youtify schedule --time 10:41   (daily automatic run)

If 'youtify' is not found, add this to ~/.zshrc:  export PATH="\$HOME/.local/bin:\$PATH"
MSG

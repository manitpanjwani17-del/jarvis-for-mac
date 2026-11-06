#!/bin/sh
# Jarvis for Mac: the installer. Used by the setup window inside Jarvis.app, and by ./setup.sh.
#
#   setup/install.sh all             everything below, in order
#   setup/install.sh check           is this Mac supported?
#   setup/install.sh python          Jarvis's own Python and packages (checksum-verified)
#   setup/install.sh models          the free AI models (checksum-verified)
#   setup/install.sh ollama          is Ollama (the free app that runs the brain) installed and running?
#   setup/install.sh install-ollama  download Ollama from ollama.com and check Ollama's signature
#   setup/install.sh brain           download the brain models through Ollama
#
# Everything goes into ~/Library/Application Support/Jarvis for Mac (or $JARVIS_HOME).
# Nothing here needs your password, an account or a key, and nothing is sent anywhere.
# Lines starting with "@" are for the setup window: @step, @progress <0-100>, @ok, @fail <why>.
set -eu

CODE="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${JARVIS_HOME:-$HOME/Library/Application Support/Jarvis for Mac}"
VENV="$HOME_DIR/venv"
PY="$VENV/bin/python"
UV_VERSION="0.13.1"
UV_SHA256="cfe454573412ff7380d15fc7dae5997ab53ffce72fce0a78e91ddbc84001acd2"
OLLAMA_TEAM="3MU9H2V9Y9"                 # Ollama Inc's Apple signing identity
OLLAMA_URL="https://ollama.com/download/Ollama-darwin.zip"
export PYTHONDONTWRITEBYTECODE=1
export JARVIS_HOME="$HOME_DIR"
# Keep uv's Python and download cache inside Jarvis's folder too, so deleting that one folder removes everything.
export UV_PYTHON_INSTALL_DIR="$HOME_DIR/python"
export UV_CACHE_DIR="$HOME_DIR/cache/uv"
export UV_PYTHON_PREFERENCE=only-managed
export UV_NO_CONFIG=1
mkdir -p "$HOME_DIR"

say() { printf '%s\n' "$*"; }
fail() { say "@fail $*"; say "✗ $*" >&2; exit 1; }

find_uv() {
  if [ -n "${JARVIS_UV:-}" ] && [ -x "$JARVIS_UV" ]; then UV="$JARVIS_UV"; return; fi   # bundled in Jarvis.app
  if [ -x "$HOME_DIR/bin/uv" ]; then UV="$HOME_DIR/bin/uv"; return; fi
  say "@step Getting uv $UV_VERSION (the tool that installs Jarvis's Python)"
  tmp="$(mktemp -d)"
  curl -fsSL --proto '=https' -o "$tmp/uv.tar.gz" \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-aarch64-apple-darwin.tar.gz" || fail "couldn't download uv"
  got="$(shasum -a 256 "$tmp/uv.tar.gz" | awk '{print $1}')"
  [ "$got" = "$UV_SHA256" ] || { rm -rf "$tmp"; fail "uv's download didn't match its fingerprint"; }
  tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
  mkdir -p "$HOME_DIR/bin"
  mv "$tmp/uv-aarch64-apple-darwin/uv" "$HOME_DIR/bin/uv"
  rm -rf "$tmp"
  UV="$HOME_DIR/bin/uv"
}

step_check() {
  say "@step Checking this Mac"
  [ "$(uname -m)" = "arm64" ] || fail "Jarvis needs a Mac with Apple silicon (M1 or later)"
  major="$(sw_vers -productVersion | cut -d. -f1)"
  [ "$major" -ge 14 ] || fail "Jarvis needs macOS 14 Sonoma or later"
  free_gb="$(df -g "$HOME" | awk 'NR==2 {print $4}')"
  [ "$free_gb" -ge 16 ] || fail "Jarvis needs about 16 GB of free space (you have $free_gb GB)"
  mem_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
  [ "$mem_gb" -ge 16 ] || say "Note: this Mac has $mem_gb GB of memory. Jarvis works best with 16 GB or more."
  say "@ok"
}

step_python() {
  find_uv
  say "@step Installing Jarvis's own Python (3.13)"
  "$UV" venv --quiet --allow-existing --python 3.13 "$VENV" || fail "couldn't set up Python"
  say "@progress 20"
  say "@step Installing Python packages (about 1.4 GB; each one checked against its fingerprint)"
  MACOSX_DEPLOYMENT_TARGET=14.0 "$UV" pip install --quiet --python "$PY" --require-hashes -r "$CODE/requirements.lock" \
    || fail "couldn't install the Python packages"
  "$UV" cache clean --quiet 2>/dev/null || true      # the downloaded packages are installed now; free that space
  say "@progress 100"
  say "@ok"
}

step_models() {
  [ -x "$PY" ] || step_python
  say "@step Downloading the free AI models (about 2.7 GB)"
  "$PY" "$CODE/setup/fetch_models.py" ${JARVIS_SETUP_JSON:+--json} || fail "a model download failed (run setup again to resume)"
  say "@ok"
}

ollama_bin() {
  for b in "$(command -v ollama 2>/dev/null || true)" /usr/local/bin/ollama /opt/homebrew/bin/ollama \
           "/Applications/Ollama.app/Contents/Resources/ollama"; do
    [ -n "$b" ] && [ -x "$b" ] && { echo "$b"; return 0; }
  done
  return 1
}

step_ollama() {
  say "@step Checking for Ollama (the free app that runs Jarvis's brain on this Mac)"
  ollama_bin >/dev/null || fail "Ollama isn't installed"
  if ! curl -fs --max-time 2 http://127.0.0.1:11434/api/version >/dev/null; then
    open -ga Ollama 2>/dev/null || true
    i=0; while [ $i -lt 30 ] && ! curl -fs --max-time 2 http://127.0.0.1:11434/api/version >/dev/null; do sleep 1; i=$((i+1)); done
  fi
  curl -fs --max-time 2 http://127.0.0.1:11434/api/version >/dev/null || fail "Ollama is installed but isn't running; open the Ollama app"
  say "@ok"
}

step_install_ollama() {
  say "@step Downloading Ollama from ollama.com (about 230 MB)"
  tmp="$(mktemp -d)"
  curl -fL --proto '=https' -o "$tmp/Ollama.zip" "$OLLAMA_URL" || { rm -rf "$tmp"; fail "couldn't download Ollama"; }
  ditto -x -k "$tmp/Ollama.zip" "$tmp/x" || { rm -rf "$tmp"; fail "couldn't open Ollama's download"; }
  app="$tmp/x/Ollama.app"
  [ -d "$app" ] || { rm -rf "$tmp"; fail "Ollama's download wasn't what was expected"; }
  # Only install it if it is really Ollama: signed by Ollama's own Apple identity, and accepted by macOS.
  codesign --verify --deep --strict "$app" 2>/dev/null || { rm -rf "$tmp"; fail "Ollama's signature didn't check out, so it wasn't installed"; }
  team="$(codesign -dv "$app" 2>&1 | sed -n 's/^TeamIdentifier=//p')"
  [ "$team" = "$OLLAMA_TEAM" ] || { rm -rf "$tmp"; fail "that download wasn't signed by Ollama, so it wasn't installed"; }
  spctl --assess --type execute "$app" 2>/dev/null || { rm -rf "$tmp"; fail "macOS didn't accept Ollama's download, so it wasn't installed"; }
  [ -e /Applications/Ollama.app ] && { rm -rf "$tmp"; say "Ollama is already in Applications"; say "@ok"; return; }
  ditto "$app" /Applications/Ollama.app || { rm -rf "$tmp"; fail "couldn't copy Ollama to Applications"; }
  rm -rf "$tmp"
  open -ga Ollama || true
  say "@ok"
}

step_brain() {
  step_ollama
  for m in "qwen3:8b" "qwen3:4b-instruct" ${JARVIS_WITH_VISION:+qwen3.5:4b}; do
    say "@step Downloading the brain model $m through Ollama"
    "$PY" - "$m" <<'EOF' || fail "couldn't download $m"
import json, sys, urllib.request
name = sys.argv[1]
req = urllib.request.Request("http://127.0.0.1:11434/api/pull", data=json.dumps({"name": name}).encode(),
                             headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=3600) as r:
    for line in r:
        msg = json.loads(line)
        if msg.get("error"):
            print("✗", msg["error"]); sys.exit(1)
        if msg.get("total"):
            print(f"@progress {int(100 * msg.get('completed', 0) / msg['total'])}", flush=True)
        if msg.get("status") == "success":
            print("✓", name, flush=True)
EOF
  done
  say "@ok"
}

case "${1:-all}" in
  check) step_check ;;
  python) step_python ;;
  models) step_models ;;
  ollama) step_ollama ;;
  install-ollama) step_install_ollama ;;
  brain) step_brain ;;
  all)
    step_check; step_python; step_models
    if ! ollama_bin >/dev/null; then
      say ""
      say "Jarvis's brain runs in Ollama, a free app. Install it from https://ollama.com/download"
      say "(or run: setup/install.sh install-ollama), then run this again."
      exit 2
    fi
    step_brain
    say ""; say "✓ Everything is installed." ;;
  *) say "unknown step: $1"; exit 64 ;;
esac

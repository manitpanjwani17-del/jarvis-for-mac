#!/bin/sh
# Jarvis for Mac, from source: builds the app on this Mac, installs it, then installs Python and the models.
#   ./setup.sh
# Needs Apple silicon, macOS 14+, about 16 GB free, and Xcode's command line tools (xcode-select --install).
set -eu
cd "$(dirname "$0")"
command -v swiftc >/dev/null || { echo "Please install Xcode's command line tools first: xcode-select --install"; exit 1; }
sh setup/install.sh check
echo "→ Building Jarvis for Mac from this source code"
sh scripts/build.sh --install
echo "→ Installing Jarvis's Python, packages and models"
JARVIS_UV="/Applications/Jarvis for Mac.app/Contents/Resources/bin/uv" sh setup/install.sh python
sh setup/install.sh models
echo
echo "✓ Done. Opening Jarvis for Mac: its setup window will get the brain (through Ollama) and walk you"
echo "  through the permissions, each with its reason."
open "/Applications/Jarvis for Mac.app"

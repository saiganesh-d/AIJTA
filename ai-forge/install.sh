#!/usr/bin/env bash
# AI Forge installer (macOS/Linux). Run: bash "<AI-Forge-Shared>/tool/install.sh"
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
FORGE_HOME="${AI_FORGE_HOME:-$HOME/.ai-forge}"
for c in python3 git copilot; do command -v "$c" >/dev/null || { echo "Missing: $c"; exit 1; }; done
python3 -c 'import sys; assert sys.version_info >= (3,11)' || { echo "Python 3.11+ required"; exit 1; }
mkdir -p "$FORGE_HOME"
[ -d "$FORGE_HOME/venv" ] || python3 -m venv "$FORGE_HOME/venv"
"$FORGE_HOME/venv/bin/pip" install --quiet --upgrade pip
# build from a local copy so pip never writes build/ or *.egg-info into the synced shared folder
rm -rf "$FORGE_HOME/tool-src" && cp -R "$HERE" "$FORGE_HOME/tool-src"
rm -rf "$FORGE_HOME/tool-src/build" "$FORGE_HOME"/tool-src/*.egg-info
"$FORGE_HOME/venv/bin/pip" install --quiet "$FORGE_HOME/tool-src"
cp "$HERE/VERSION" "$FORGE_HOME/installed_version"
"$FORGE_HOME/venv/bin/forge" setup
echo "Done. Try: forge doctor --live"

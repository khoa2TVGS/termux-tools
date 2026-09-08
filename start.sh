#!/usr/bin/env bash
# ==============================================================================
# Termux TailMedia Launcher Script
# ==============================================================================
# 1. Acquires Android Termux wake-lock to prevent CPU sleep when screen is off
# 2. Verifies Android storage access (/storage/emulated/0)
# 3. Launches the multithreaded media server on port 8080
# ==============================================================================

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "======================================================"
echo "  🚀 Starting Termux Tailscale Media Server"
echo "======================================================"

# 1. Prevent Android from killing/throttling the server when the screen is off
if command -v termux-wake-lock >/dev/null 2>&1; then
    echo "⚡ Acquiring Termux wake lock (keeps server alive with screen off)..."
    termux-wake-lock
    trap 'echo "Releasing wake lock..."; termux-wake-unlock' EXIT
fi

# 2. Verify Storage Permission
STORAGE_DIR="/storage/emulated/0"
if [ ! -d "$STORAGE_DIR" ] || [ ! -r "$STORAGE_DIR" ]; then
    echo "⚠️  WARNING: Cannot read $STORAGE_DIR"
    echo "   Please grant storage access by running:"
    echo "   👉 termux-setup-storage"
    echo ""
    read -p "Press Enter after granting storage permission, or Ctrl+C to abort..."
fi

# 3. Check for Python
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ Python 3 is not installed."
    echo "   Install it with: pkg install python"
    exit 1
fi

# 4. Optional Pillow Check
if ! python3 -c "import PIL" >/dev/null 2>&1; then
    echo "💡 Tip: For faster thumbnail generation, install Pillow via:"
    echo "   pkg install python-pillow"
    echo ""
fi

# 5. Launch Server
PORT="${PORT:-8080}"
ROOT_DIR="${MEDIA_ROOT:-/storage/emulated/0}"

echo "Starting server on port $PORT pointing to $ROOT_DIR..."
exec python3 server.py --root "$ROOT_DIR" --port "$PORT" "$@"

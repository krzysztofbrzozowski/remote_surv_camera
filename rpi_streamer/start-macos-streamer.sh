#!/usr/bin/env bash
# Start the macOS camera streamer natively (Docker can't access camera on macOS)
#
# Usage:
#   ./start-macos-streamer.sh
#
# Prerequisites:
#   - Server must be running (cd server && docker compose up -d)
#   - Terminal.app must have camera permission (System Settings > Privacy > Camera)

set -e

cd "$(dirname "$0")"

echo "==> Setting up Python venv..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate
pip install --quiet -r requirements.txt

echo "==> Starting macOS camera streamer..."
echo "    Camera: macos-cam"
echo "    Server: ws://localhost:8000"
echo ""
echo "Press Ctrl+C to stop"
echo ""

STREAMER_CONFIG=configs/macos-cam.yaml python3 main.py

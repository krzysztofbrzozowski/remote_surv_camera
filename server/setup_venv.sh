#!/usr/bin/env bash
# Create and configure a Python venv for running the server on the host (no Docker).
# Useful for local WebRTC development where Docker Desktop macOS can't forward UDP.
#
# Usage:
#   ./setup_venv.sh          # Create venv and install deps
#   source venv/bin/activate  # Activate

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="venv"

if [ -d "$VENV_DIR" ]; then
    echo "ℹ️  venv already exists at $VENV_DIR"
    echo "   To recreate: rm -rf $VENV_DIR && $0"
else
    echo "🐍 Creating venv..."
    python3 -m venv "$VENV_DIR"
fi

echo "📦 Installing dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r requirements.txt --quiet

echo ""
echo "✅ venv ready at: $SCRIPT_DIR/$VENV_DIR"
echo ""
echo "To activate:  source $VENV_DIR/bin/activate"
echo "To run server:"
echo "  export DJANGO_SECRET_KEY='dev-secret-key'"
echo "  export CAMERA_TOKEN='<your-camera-token>'"
echo "  export DJANGO_ALLOWED_HOSTS='*'"
echo "  export DJANGO_DEBUG=1"
echo "  python manage.py migrate"
echo "  python manage.py createsuperuser  # (or use admin/admin from Docker)"
echo "  daphne -b 0.0.0.0 -p 8080 surv_server.asgi:application"

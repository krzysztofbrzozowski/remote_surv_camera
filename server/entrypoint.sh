#!/bin/sh
# Fix ownership of mounted volumes (created as root by Docker)
chown -R appuser:appuser /app/data /app/recordings /app/logs /app/staticfiles 2>/dev/null || true

# Drop to appuser and exec the CMD
exec su -s /bin/sh appuser -c "$*"

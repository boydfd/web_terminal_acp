#!/usr/bin/env bash
set -euo pipefail

echo "Running database migrations..."
alembic upgrade head

if [ "$#" -eq 0 ]; then
  set -- uvicorn app.main:app \
    --host "${APP_HOST:-0.0.0.0}" \
    --port "${APP_PORT:-8000}" \
    --ws-ping-interval "${WS_PING_INTERVAL:-30}" \
    --ws-ping-timeout "${WS_PING_TIMEOUT:-120}"
fi

echo "Starting backend..."
exec "$@"

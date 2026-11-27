#!/usr/bin/env bash
set -e

echo "[notification-entrypoint] Applying Alembic database migrations..."
if python -m alembic upgrade head; then
    echo "[notification-entrypoint] Database migrations applied successfully."
else
    echo "[notification-entrypoint] WARNING: Alembic migrations failed or database connection is pending. Continuing startup..."
fi

exec "$@"

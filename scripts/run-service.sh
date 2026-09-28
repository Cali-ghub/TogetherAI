#!/usr/bin/env bash
# Load the setup script's shell-escaped environment, then start the web service.
set -euo pipefail

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${TOGETHER_ENV_FILE:-/etc/togetherai/env}"
PYTHON="${TOGETHER_PYTHON:-$APP_ROOT/.venv/bin/python}"
HOST="${TOGETHER_HOST:-127.0.0.1}"
PORT="${TOGETHER_PORT:-8790}"

if [[ ! -r "$ENV_FILE" ]]; then
  printf 'Cannot read TogetherAI environment file: %s\n' "$ENV_FILE" >&2
  exit 1
fi
if [[ ! -x "$PYTHON" ]]; then
  printf 'Python executable is unavailable: %s\n' "$PYTHON" >&2
  exit 1
fi

set -a
# This is the shell-escaped format written by set-local-secrets.sh.
source "$ENV_FILE"
set +a
cd "$APP_ROOT"
exec "$PYTHON" -m uvicorn app.main:app --host "$HOST" --port "$PORT"

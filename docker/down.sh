#!/usr/bin/env bash
# Stop agentic's Docker container.  Usage:  ./docker/down.sh [extra compose args]
#
# Wraps `docker compose down`. Run from anywhere. Your state dir and project repos
# are untouched — only the container (and its network) are removed.
#
#   ./docker/down.sh            # stop + remove the container
#   ./docker/down.sh --volumes  # also remove any named volumes (none by default)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$DIR/.env"
COMPOSE="$DIR/docker-compose.yml"

# down works even without .env, but pass it when present so var interpolation
# doesn't warn.
if [[ -f "$ENV_FILE" ]]; then
  exec docker compose --env-file "$ENV_FILE" -f "$COMPOSE" down "$@"
else
  exec docker compose -f "$COMPOSE" down "$@"
fi

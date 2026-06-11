#!/usr/bin/env bash
# Start agentic in Docker.  Usage:  ./docker/up.sh [--build] [-d|--detach] [extra compose args]
#
# Wraps `docker compose up` with the right --env-file / -f so you don't have to
# type them. Run from anywhere — it resolves its own directory.
#
#   ./docker/up.sh            # foreground (logs in your terminal; Ctrl-C stops)
#   ./docker/up.sh --build    # rebuild the image first (after a code/Dockerfile change)
#   ./docker/up.sh -d         # detached (runs in the background)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$DIR/.env"
COMPOSE="$DIR/docker-compose.yml"

# .env must exist — it's written by the wizard. Point there if missing.
if [[ ! -f "$ENV_FILE" ]]; then
  echo "❌ $ENV_FILE not found. Run the setup wizard first:" >&2
  echo "     $DIR/setup.sh" >&2
  exit 1
fi

# Port-collision guard: if HOST_PORT is already in use (commonly a NATIVE agentic
# server, or a previous container), the container's port bind would fail or you'd
# end up looking at the wrong server. Refuse early with an actionable message.
# (Skipped when bringing an existing container back up — see container check.)
_port="$(grep -E '^HOST_PORT=' "$ENV_FILE" | cut -d= -f2- || true)"; _port="${_port:-4080}"
# Is OUR container already the one running? Then a re-up is fine.
_own_container="$(docker ps --filter name=agentic --filter status=running -q 2>/dev/null || true)"
if [[ -z "$_own_container" ]] && command -v lsof >/dev/null 2>&1; then
  if lsof -nP -iTCP:"$_port" -sTCP:LISTEN >/dev/null 2>&1; then
    _who="$(lsof -nP -iTCP:"$_port" -sTCP:LISTEN 2>/dev/null | awk 'NR==2{print $1" (pid "$2")"}')"
    echo "❌ Port $_port is already in use by: ${_who:-another process}." >&2
    echo "   Something else owns it — likely a native 'agentic serve' on this port." >&2
    echo "   Either stop it:        agentic serve stop" >&2
    echo "   …or use another port:  set HOST_PORT in $ENV_FILE (e.g. 4081), then retry." >&2
    echo "   Running the container AND a native server on the same port/state corrupts the queue." >&2
    exit 1
  fi
fi

# Friendly preflight: warn (don't block) if the host Ollama isn't reachable on
# all interfaces, since local-mode jobs need it. The container reaches the host
# at host.docker.internal, which maps to the host's LAN IP.
_ollama="$(grep -E '^OLLAMA_HOST=' "$ENV_FILE" | cut -d= -f2- || true)"
if command -v curl >/dev/null 2>&1; then
  _ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  if [[ -n "$_ip" ]] && ! curl -fsS --max-time 2 "http://$_ip:11434/api/tags" >/dev/null 2>&1; then
    echo "⚠️  Host Ollama not reachable on all interfaces — local-mode jobs will fail." >&2
    echo "    Start it with:  OLLAMA_HOST=0.0.0.0:11434 ollama serve" >&2
    echo "    (continuing — the dashboard will still come up)" >&2
  fi
fi

exec docker compose --env-file "$ENV_FILE" -f "$COMPOSE" up "$@"

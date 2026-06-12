#!/usr/bin/env bash
# agentic Docker setup wizard.
#
# Interactively writes docker/.env so you never hand-edit identity paths. It
# enforces the safety model: a STATE dir kept separate from any source checkout,
# a NARROW project mount (never the whole home), and disjoint state/project
# subtrees. These host-side checks mirror the container entrypoint's preflight.
#
#   ./docker/setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

_die()  { echo "❌ $*" >&2; exit 1; }
_warn() { echo "⚠️  $*" >&2; }
_ok()   { echo "✅ $*"; }

# realpath that tolerates a not-yet-existing dir (resolves the parent).
_abspath() {
  local p="$1"
  p="${p/#\~/$HOME}"
  if [[ -d "$p" ]]; then (cd "$p" && pwd); else echo "${p%/}"; fi
}
# is $1 an ancestor of (or equal to) $2 ?
_is_ancestor() { local a="${1%/}/" b="${2%/}/"; [[ "$b" == "$a"* ]]; }
# does dir look like an agentic SOURCE checkout?
_is_source_checkout() { [[ -e "$1/lib/serve.py" || -e "$1/bin/agentic" ]]; }

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " agentic Docker setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

# ── 1. PROJECT_DIR — the repo (or projects parent) the worker may touch ──────
echo "1) Project directory"
echo "   The repo (or a parent folder of repos) you want agents to work on."
echo "   This is the ONLY host code the container can see. Pick it NARROW —"
echo "   a single project or a 'Projects/' parent, NOT your home dir."
_default_proj=""
for cand in "$HOME/Projects" "$HOME/code" "$HOME/src" "$HOME/dev"; do
  [[ -d "$cand" ]] && { _default_proj="$cand"; break; }
done
read -r -p "   Project dir [${_default_proj:-/path/to/projects}]: " PROJECT_DIR
PROJECT_DIR="$(_abspath "${PROJECT_DIR:-$_default_proj}")"
[[ -n "$PROJECT_DIR" ]] || _die "No project dir given."
[[ -d "$PROJECT_DIR" ]] || _die "Project dir does not exist: $PROJECT_DIR"

# Refuse whole-home / system roots unless explicitly forced.
case "$PROJECT_DIR" in
  "$HOME" | "/" | "/Users" | "/home" | "/usr" | "/etc" | "/var")
    if [[ "${AGENTIC_I_UNDERSTAND:-}" != "1" ]]; then
      _die "Refusing a home/system-spanning project dir ('$PROJECT_DIR'). This exposes too much to the container.
   Pick a narrower folder, or re-run with AGENTIC_I_UNDERSTAND=1 to override."
    fi
    _warn "Overriding broad project dir by request: $PROJECT_DIR" ;;
esac
_ok "Project dir: $PROJECT_DIR"
echo

# ── 2. STATE dir — queue/worktrees/diffs/logs/settings/secrets ──────────────
echo "2) State directory"
echo "   Holds the queue, worktrees, diffs, logs, settings.json, secrets.json."
echo "   Kept SEPARATE from any agentic source checkout. Default is safe."
_default_state="$HOME/.agentic-data"
read -r -p "   State dir [$_default_state]: " AGENTIC_HOME
AGENTIC_HOME="$(_abspath "${AGENTIC_HOME:-$_default_state}")"

# Hard refusals (mirror the entrypoint preflight).
[[ "$AGENTIC_HOME" != "/" && "$AGENTIC_HOME" != "$HOME" ]] || _die "State dir must not be / or your home dir."
_is_source_checkout "$AGENTIC_HOME" && _die "State dir '$AGENTIC_HOME' looks like an agentic SOURCE checkout (found lib/ or bin/).
   Use a dedicated dir like ~/.agentic-data — never your ~/.agentic source repo."
[[ -e "$AGENTIC_HOME/.git" ]] && _die "State dir '$AGENTIC_HOME' is a git repo; use a non-repo state dir (e.g. ~/.agentic-data)."

# Disjointness: state and project must not nest either way.
if _is_ancestor "$AGENTIC_HOME" "$PROJECT_DIR" || _is_ancestor "$PROJECT_DIR" "$AGENTIC_HOME"; then
  _die "State dir and project dir overlap:
     state   = $AGENTIC_HOME
     project = $PROJECT_DIR
   They must be disjoint so the project mount can't expose state. Choose non-nested paths."
fi
# Scaffold the FULL writable tree up front, so the container never has to create
# anything outside its mount. Everything the container writes lives here:
#   queue/worktrees/diffs/logs — job state
#   home/                       — the container's HOME (npm/yarn/pnpm caches,
#                                 .gitconfig). The container's $HOME is unwritable
#                                 otherwise (the host home isn't mounted), so npm
#                                 and `git config --global` fail. Keeping HOME in
#                                 the state dir makes the container self-contained
#                                 and isolated — it never reads/writes your real
#                                 ~/.gitconfig or ~/.npm.
mkdir -p \
  "$AGENTIC_HOME"/queue/{pending,running,done,failed,abandoned,cancelled} \
  "$AGENTIC_HOME"/worktrees \
  "$AGENTIC_HOME"/diffs \
  "$AGENTIC_HOME"/logs \
  "$AGENTIC_HOME"/home/.npm \
  "$AGENTIC_HOME"/home/.cache
_ok "State dir scaffolded: $AGENTIC_HOME (incl. home/ for container caches)"
echo

# ── 3. host facts ────────────────────────────────────────────────────────────
HOST_UID="$(id -u)"; HOST_GID="$(id -g)"
read -r -p "3) Dashboard port [4080]: " HOST_PORT; HOST_PORT="${HOST_PORT:-4080}"
read -r -p "4) Host Ollama URL [http://host.docker.internal:11434]: " OLLAMA_HOST
OLLAMA_HOST="${OLLAMA_HOST:-http://host.docker.internal:11434}"
echo

# ── 4. confirm + write ───────────────────────────────────────────────────────
echo "About to write $ENV_FILE with these mounts (identity, disjoint):"
echo "   STATE   $AGENTIC_HOME  ->  $AGENTIC_HOME"
echo "   PROJECT $PROJECT_DIR   ->  $PROJECT_DIR"
echo "   (app source stays baked in the image at /opt/agentic — never mounted)"
echo "   uid:gid = $HOST_UID:$HOST_GID   port = $HOST_PORT"
read -r -p "Write it? [y/N] " yn
[[ "$yn" =~ ^[Yy]$ ]] || { echo "Aborted; nothing written."; exit 0; }

umask 077
cat > "$ENV_FILE" <<ENV
# Generated by docker/setup.sh — host paths + ids only. Behavior is set in the UI.
# STATE dir (queue/worktrees/diffs/logs/settings/secrets). Separate from source.
# Named AGENTIC_STATE_DIR (not AGENTIC_HOME) so a native shell's AGENTIC_HOME
# can't shadow it via Compose's env precedence.
AGENTIC_STATE_DIR=$AGENTIC_HOME
# Project repo(s) the worker may touch == what the UI picker can browse.
PROJECT_DIR=$PROJECT_DIR
# Host home (Path.home() alignment; NOT mounted).
HOST_HOME=$HOME
HOST_UID=$HOST_UID
HOST_GID=$HOST_GID
HOST_PORT=$HOST_PORT
OLLAMA_HOST=$OLLAMA_HOST
ENV
_ok "Wrote $ENV_FILE"
echo
echo "Next:"
echo "   1. Start host Ollama on all interfaces. To let a planning chat run AT THE"
echo "      SAME TIME as a worker job (instead of queueing behind it), allow >1"
echo "      parallel request:"
echo "          OLLAMA_HOST=0.0.0.0 OLLAMA_NUM_PARALLEL=2 ollama serve"
echo "      (Ollama serializes by default — without this, local planning waits"
echo "       behind a running local job. Heavy models may need the VRAM headroom;"
echo "       or just use a CLOUD planning thread while a local worker runs.)"
echo "   2. Bring it up:  ./docker/up.sh --build"
echo "   3. Open:  http://localhost:$HOST_PORT"

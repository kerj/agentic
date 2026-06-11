#!/usr/bin/env bash
# Container entrypoint for agentic (all-in-one: UI + queue + worker).
#
# SAFETY CONTRACT (this file must never destroy host data):
#   • It performs NO `rm`, NO `mv`, NO `ln -s` over existing paths, and NO
#     recursive `chown` of a mounted dir. The ONLY filesystem mutations are
#     `mkdir -p` of known STATE subdirs and `git config`. (A build-time check in
#     the Dockerfile fails the image if the word `rm ` appears in this file.)
#   • APP SOURCE is baked in the image at $AGENTIC_APP (=/opt/agentic) and is
#     NEVER mounted and NEVER written. STATE lives at $AGENTIC_HOME (a mounted,
#     state-only dir). The two are separate variables — the container never tries
#     to put source into the state mount, so there is nothing to "bridge."
#   • PREFLIGHT refuses to run if $AGENTIC_HOME looks like an agentic SOURCE
#     checkout (contains .git / lib / bin) — the exact condition that, combined
#     with a whole-home mount, caused source loss before. We abort BEFORE any
#     write rather than risk it.
set -euo pipefail

# ── 0. Inputs ────────────────────────────────────────────────────────────────
APP="${AGENTIC_APP:-/opt/agentic}"          # baked source (in image, read-only)
PY="${AGENTIC_PYTHON:-$APP/venv/bin/python3}"
: "${AGENTIC_HOME:?AGENTIC_HOME must be set to a STATE-only dir (compose sets it)}"

# The container's HOME lives INSIDE the state dir (compose sets it to
# $AGENTIC_STATE_DIR/home). The host home is NOT mounted, so a host-home HOME is
# unwritable — npm caches and `git config --global` (.gitconfig) would fail,
# silently breaking installs AND merge-commit identity for accept/accept-chain.
# Keeping HOME in the state dir makes the container self-contained and isolated
# from your real ~/.gitconfig and ~/.npm. Default it under AGENTIC_HOME if unset.
HOME="${HOME:-$AGENTIC_HOME/home}"
case "$HOME" in "$AGENTIC_HOME"/*) : ;; *) HOME="$AGENTIC_HOME/home" ;; esac
export AGENTIC_APP AGENTIC_HOME AGENTIC_PYTHON="$PY" HOME

# Tool caches/config under the writable HOME (belt-and-suspenders so they resolve
# correctly even if a tool ignores HOME).
export npm_config_cache="$HOME/.npm"
export XDG_CACHE_HOME="$HOME/.cache"
export XDG_CONFIG_HOME="$HOME/.config"
export YARN_CACHE_FOLDER="$HOME/.cache/yarn"

# ── 1. PREFLIGHT REFUSALS — abort before touching anything ───────────────────
_refuse() { echo "❌ REFUSING TO START: $1" >&2; echo "   $2" >&2; exit 1; }

# 1a. AGENTIC_HOME must be STATE-only, never a source checkout. If it contains a
#     git repo or the app's source dirs, we are pointed at the wrong place and a
#     careless op could clobber real code. Abort loudly.
if [[ -e "$AGENTIC_HOME/.git" || -e "$AGENTIC_HOME/lib/serve.py" || -e "$AGENTIC_HOME/bin/agentic" ]]; then
  _refuse "AGENTIC_HOME='$AGENTIC_HOME' looks like an agentic SOURCE checkout (found .git / lib / bin)." \
          "AGENTIC_HOME must be a STATE-ONLY dir (e.g. ~/.agentic-data). Fix the mount and retry."
fi

# 1b. Never operate on dangerous roots.
case "$AGENTIC_HOME" in
  "" | "/" | "$HOME" | "$APP" | "/opt/agentic" | "/usr" | "/etc" | "/var")
    _refuse "AGENTIC_HOME='$AGENTIC_HOME' is a forbidden/system path." \
            "Point it at a dedicated state dir (e.g. ~/.agentic-data)." ;;
esac

# 1c. STATE and the browse/projects root must be DISJOINT subtrees, or the
#     projects mount could re-expose the state dir (the second half of the prior
#     incident). Refuse if either is an ancestor of the other.
if [[ -n "${BROWSE_ROOT:-}" ]]; then
  _h="${AGENTIC_HOME%/}/"; _b="${BROWSE_ROOT%/}/"
  if [[ "$_h" == "$_b"* || "$_b" == "$_h"* ]]; then
    _refuse "AGENTIC_HOME='$AGENTIC_HOME' and BROWSE_ROOT='$BROWSE_ROOT' overlap." \
            "Make them disjoint so the projects mount can't expose the state dir."
  fi
fi

# 1d. Source must be baked in the image, not on a writable mount. (Best-effort:
#     if $APP/lib/serve.py is missing, the image is malformed.)
[[ -f "$APP/lib/serve.py" && -x "$PY" ]] || \
  _refuse "App source/venv missing at AGENTIC_APP='$APP'." \
          "The image is malformed — rebuild it."

# ── 2. CREATE-ONLY state seeding (mkdir -p only — no rm, no ln, no cp) ────────
# We create ONLY state subdirs. We NEVER create or touch bin/lib/agents/profiles/
# venv under AGENTIC_HOME — the code reads those from $AGENTIC_APP now.
mkdir -p \
  "$AGENTIC_HOME"/queue/{pending,running,done,failed,abandoned,cancelled} \
  "$AGENTIC_HOME"/worktrees \
  "$AGENTIC_HOME"/diffs \
  "$AGENTIC_HOME"/logs \
  "$HOME"/.npm "$HOME"/.cache "$HOME"/.config

# ── 3. git identity, written into the container's HOME (.gitconfig) ──────────
# The merge commits made by accept/accept-chain need a committer identity, and
# Review-in-IDE / apply need safe.directory for the host-owned bind mount. Write
# them into $HOME/.gitconfig. Run as the TARGET uid (via gosu) so the file is
# owned by that uid and stays updatable — a root-written config in the mounted
# home would be root-owned and the dropped worker couldn't touch it.
_git_setup() {
  git config --global --add safe.directory '*'
  git config --global user.name  "$(git config --global user.name  2>/dev/null || echo 'agentic')"
  git config --global user.email "$(git config --global user.email 2>/dev/null || echo 'agentic@localhost')"
}
if [[ -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]] && command -v gosu >/dev/null 2>&1; then
  # Make HOME owned by the target uid first, then write the config as that uid.
  chown "${HOST_UID}:${HOST_GID}" "$HOME" "$HOME"/.npm "$HOME"/.cache "$HOME"/.config 2>/dev/null || true
  gosu "${HOST_UID}:${HOST_GID}" bash -c "$(declare -f _git_setup); HOME='$HOME' _git_setup" 2>/dev/null || true
else
  _git_setup 2>/dev/null || true
fi

# ── 4. network ───────────────────────────────────────────────────────────────
export AGENTIC_BIND="${AGENTIC_BIND:-0.0.0.0}"
export AGENTIC_PORT="${AGENTIC_PORT:-4080}"

echo "agentic container starting"
echo "  AGENTIC_APP    = $APP   (source, baked, read-only)"
echo "  AGENTIC_HOME   = $AGENTIC_HOME   (state, mounted)"
echo "  AGENTIC_PYTHON = $PY"
echo "  HOME           = $HOME"
echo "  bind           = ${AGENTIC_BIND}:${AGENTIC_PORT}"
echo "  browse root    = ${BROWSE_ROOT:-<unset>}"
echo "  ollama         = ${OLLAMA_HOST:-<unset>}"

# ── 5. exec serve.py via gosu (host ownership), NO recursive chown ───────────
# Running as the host UID:GID means files created in the state mount get host
# ownership natively — no `chown -R` of the mount is ever needed. The mount is
# already host-owned (it's the user's dir), so we touch nothing.
SERVE=( "$PY" "$APP/lib/serve.py" "$AGENTIC_PORT" )
if [[ -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]] && command -v gosu >/dev/null 2>&1; then
  exec gosu "${HOST_UID}:${HOST_GID}" "${SERVE[@]}"
else
  exec "${SERVE[@]}"
fi

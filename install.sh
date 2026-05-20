#!/bin/bash
# Agentic installer

set -euo pipefail

AGENTIC_HOME="${HOME}/.agentic"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing agentic..."
echo ""

# ── Directory structure ────────────────────────────────────────────────────────
mkdir -p "${AGENTIC_HOME}"/{bin,lib,agents}
mkdir -p "${HOME}/.agentic/queue"/{pending,running,done,failed,abandoned,cancelled}
mkdir -p "${HOME}/.agentic/worktrees"
echo "✅ Directories created"

# ── Copy files ─────────────────────────────────────────────────────────────────
cp -r "${REPO_DIR}/bin/"*    "${AGENTIC_HOME}/bin/"
cp -r "${REPO_DIR}/lib/"*    "${AGENTIC_HOME}/lib/"
cp -r "${REPO_DIR}/agents/"* "${AGENTIC_HOME}/agents/" 2>/dev/null || true
chmod +x "${AGENTIC_HOME}/bin/agentic"
echo "✅ Files installed"

# ── Shell config ───────────────────────────────────────────────────────────────
if ! grep -q "AGENTIC_HOME" ~/.zshrc 2>/dev/null; then
  cat >> ~/.zshrc << 'EOF'

# agentic
export AGENTIC_HOME="$HOME/.agentic"
export PATH="$AGENTIC_HOME/bin:$PATH"
[[ -f "$AGENTIC_HOME/.agentic.conf" ]] && source "$AGENTIC_HOME/.agentic.conf"
EOF
  echo "✅ Added to .zshrc"
else
  echo "✅ Shell already configured"
fi

# ── Dependency checks ──────────────────────────────────────────────────────────
echo ""
echo "Checking dependencies..."

_ok() { echo "  ✅ $1"; }
_warn() { echo "  ⚠️  $1"; }

command -v jq      &>/dev/null && _ok "jq"          || _warn "jq not found — install: brew install jq"
command -v python3 &>/dev/null && _ok "python3"     || _warn "python3 not found"
command -v git     &>/dev/null && _ok "git"         || _warn "git not found"
command -v claude  &>/dev/null && _ok "claude CLI"  || _warn "Claude Code CLI not found — required for running jobs
     Install: https://claude.ai/code
     After installing, run: claude (to authenticate)"

echo ""

# ── Done ───────────────────────────────────────────────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Done"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Next steps:"
echo "  1. source ~/.zshrc"
if ! command -v claude &>/dev/null; then
echo "  2. Install Claude Code: https://claude.ai/code"
echo "  3. cd your-project && agentic serve"
else
echo "  2. cd your-project && agentic serve"
fi
echo ""

#!/bin/bash
# Agentic Workflow Installer

set -euo pipefail

AGENTIC_HOME="${HOME}/.agentic"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🚀 Installing Agentic Workflow..."
echo ""

# ============================================================
# 1. CREATE DIRECTORY STRUCTURE
# ============================================================
mkdir -p "${AGENTIC_HOME}"/{bin,lib,agents}
echo "✅ Created ${AGENTIC_HOME}"

# ============================================================
# 2. COPY FILES FROM REPO
# ============================================================
cp -r "${REPO_DIR}/bin/"*    "${AGENTIC_HOME}/bin/"
cp -r "${REPO_DIR}/lib/"*    "${AGENTIC_HOME}/lib/"
cp -r "${REPO_DIR}/agents/"* "${AGENTIC_HOME}/agents/" 2>/dev/null || true

chmod +x "${AGENTIC_HOME}/bin/agentic"
chmod +x "${AGENTIC_HOME}/bin/agentic-switch"
echo "✅ Copied and configured files"

# ============================================================
# 3. CONFIG SETUP
# ============================================================
CONF_FILE="${AGENTIC_HOME}/.agentic.conf"
EXAMPLE_FILE="${REPO_DIR}/.agentic.conf.example"

if [[ -f "$CONF_FILE" ]]; then
  echo "✅ Config already exists at ${CONF_FILE}"
else
  if [[ -f "$EXAMPLE_FILE" ]]; then
    cp "$EXAMPLE_FILE" "$CONF_FILE"
    chmod 600 "$CONF_FILE"
    echo "📋 Created config from example"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Configuration Setup"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "Choose your provider:"
    echo "  1. Anthropic API (claude-opus-4, cloud)"
    echo "  2. Ollama (local models)"
    echo ""
    read -p "Choice (1/2): " provider_choice

    case "$provider_choice" in
      1)
        read -p "Anthropic API Key (sk-ant-...): " api_key
        if [[ -n "$api_key" ]]; then
          cat > "$CONF_FILE" << EOF
# ── Anthropic ──────────────────────────────────────────────────────────────
export AGENTIC_MODEL="claude-opus-4-20250514"
export ANTHROPIC_BASE_URL="https://api.anthropic.com"
export ANTHROPIC_API_KEY="$api_key"
unset ANTHROPIC_AUTH_TOKEN

export OLLAMA_MAX_LOADED_MODELS=3
export OLLAMA_KEEP_ALIVE="30m"
EOF
          chmod 600 "$CONF_FILE"
          echo "✅ Configured for Anthropic API"
        else
          echo "⚠️  No key entered — edit manually: ${CONF_FILE}"
        fi
        ;;
      2)
        cat > "$CONF_FILE" << 'EOF'
# ── Ollama / Qwen ──────────────────────────────────────────────────────────
export AGENTIC_MODEL="qwen2.5-coder:32b"
export ANTHROPIC_BASE_URL="http://localhost:11434"
export ANTHROPIC_AUTH_TOKEN="ollama"
unset ANTHROPIC_API_KEY

export OLLAMA_MAX_LOADED_MODELS=3
export OLLAMA_KEEP_ALIVE="30m"
EOF
        chmod 600 "$CONF_FILE"
        echo "✅ Configured for Ollama"
        ;;
      *)
        echo "⚠️  Skipped — edit manually: ${CONF_FILE}"
        ;;
    esac
  else
    echo "⚠️  No .agentic.conf.example found — creating minimal config"
    cat > "$CONF_FILE" << 'EOF'
# Agentic Workflow Configuration
# This file contains secrets — never commit it to git

ANTHROPIC_API_KEY=
AGENTIC_MODEL=claude-opus-4-20250514
ANTHROPIC_BASE_URL=https://api.anthropic.com
EOF
    chmod 600 "$CONF_FILE"
    echo "   Edit before use: ${CONF_FILE}"
  fi
fi

# ============================================================
# 4. SHELL CONFIGURATION
# ============================================================
if ! grep -q "AGENTIC_HOME" ~/.zshrc 2>/dev/null; then
  cat >> ~/.zshrc << 'EOF'

# ================================
# Agentic Workflow
# ================================

export AGENTIC_HOME="$HOME/.agentic"
export PATH="$AGENTIC_HOME/bin:$PATH"

# Load configuration
[[ -f "$AGENTIC_HOME/.agentic.conf" ]] && source "$AGENTIC_HOME/.agentic.conf"

# ================================
EOF
  echo "✅ Added to .zshrc"
else
  echo "✅ Already in .zshrc"
fi

# ============================================================
# 5. DONE
# ============================================================
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Installation Complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Next steps:"
echo "  1. Reload shell:          source ~/.zshrc"
echo "  2. Verify config:         agentic switch status"
echo "  3. Initialize project:    cd your-project && agentic init"
echo "  4. Start workflow:        agentic"
echo ""
#!/bin/bash
# Configuration management

AGENTIC_CONF="${AGENTIC_HOME}/.agentic.conf"

# Default configuration
AGENTIC_MODEL="${AGENTIC_MODEL:-qwen2.5-coder:32b}"
AGENTIC_AUTH_TOKEN="${AGENTIC_AUTH_TOKEN:-ollama}"
AGENTIC_BASE_URL="${AGENTIC_BASE_URL:-http://localhost:11434}"
AGENTIC_API_KEY="${AGENTIC_API_KEY:-}"
OLLAMA_MAX_LOADED_MODELS="${OLLAMA_MAX_LOADED_MODELS:-3}"
OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-30m}"

function agentic-config() {
  echo "⚙️  Agentic Configuration"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  
  # Load existing config if present
  if [[ -f "$AGENTIC_CONF" ]]; then
    source "$AGENTIC_CONF"
    echo "Current configuration loaded from: $AGENTIC_CONF"
    echo ""
  fi
  
  echo "Current settings:"
  echo "  Model: $AGENTIC_MODEL"
  echo "  Base URL: $AGENTIC_BASE_URL"
  echo "  API Key: ${AGENTIC_API_KEY:-[not set]}"
  echo "  Auth Token: ${ANTHROPIC_AUTH_TOKEN:-[not set]}"
  echo "  Ollama Max Models: $OLLAMA_MAX_LOADED_MODELS"
  echo "  Ollama Keep Alive: $OLLAMA_KEEP_ALIVE"
  echo ""
  
  read -p "Update configuration? (y/n) " update_config
  if [[ ! "$update_config" =~ ^[Yy]$ ]]; then
    echo "Configuration unchanged"
    return 0
  fi
  
  echo ""
  echo "Enter new values (press Enter to keep current):"
  echo ""
  
  # Model
  read -p "Model name [$AGENTIC_MODEL]: " new_model
  AGENTIC_MODEL="${new_model:-$AGENTIC_MODEL}"
  
  # Base URL (this determines Anthropic vs Ollama)
  read -p "Base URL [$AGENTIC_BASE_URL]: " new_url
  AGENTIC_BASE_URL="${new_url:-$AGENTIC_BASE_URL}"
  
  # Detect if using Anthropic API or Ollama
  if [[ "$AGENTIC_BASE_URL" == *"anthropic.com"* ]]; then
    # Using Anthropic API - need API key, NOT auth token
    echo ""
    echo "Detected Anthropic API - API Key required"
    read -p "API Key [${AGENTIC_API_KEY:-not set}]: " new_key
    AGENTIC_API_KEY="${new_key:-$AGENTIC_API_KEY}"
    AGENTIC_AUTH_TOKEN=""  # Clear auth token for Anthropic
  else
    # Using Ollama or other - need auth token
    echo ""
    echo "Detected Ollama/Local - Auth Token required"
    read -p "Auth token [$AGENTIC_AUTH_TOKEN]: " new_auth
    AGENTIC_AUTH_TOKEN="${new_auth:-$AGENTIC_AUTH_TOKEN}"
  fi
  
  # Ollama settings
  read -p "Ollama max loaded models [$OLLAMA_MAX_LOADED_MODELS]: " new_max
  OLLAMA_MAX_LOADED_MODELS="${new_max:-$OLLAMA_MAX_LOADED_MODELS}"
  
  read -p "Ollama keep alive [$OLLAMA_KEEP_ALIVE]: " new_keep
  OLLAMA_KEEP_ALIVE="${new_keep:-$OLLAMA_KEEP_ALIVE}"
  
  # Save configuration based on backend type
  if [[ "$AGENTIC_BASE_URL" == *"anthropic.com"* ]]; then
    # Anthropic API config
    cat > "$AGENTIC_CONF" <<CONFIG
# Agentic Workflow Configuration
# Generated: $(date)
# Backend: Anthropic API

export AGENTIC_MODEL="$AGENTIC_MODEL"
export ANTHROPIC_BASE_URL="$AGENTIC_BASE_URL"
export ANTHROPIC_API_KEY="$AGENTIC_API_KEY"

# Unset AUTH_TOKEN for Anthropic API (prevents conflicts)
unset ANTHROPIC_AUTH_TOKEN

export OLLAMA_MAX_LOADED_MODELS=$OLLAMA_MAX_LOADED_MODELS
export OLLAMA_KEEP_ALIVE="$OLLAMA_KEEP_ALIVE"
CONFIG
  else
    # Ollama/Local config
    cat > "$AGENTIC_CONF" <<CONFIG
# Agentic Workflow Configuration
# Generated: $(date)
# Backend: Ollama/Local

export AGENTIC_MODEL="$AGENTIC_MODEL"
export ANTHROPIC_BASE_URL="$AGENTIC_BASE_URL"
export ANTHROPIC_AUTH_TOKEN="$AGENTIC_AUTH_TOKEN"

# Unset API_KEY for Ollama (not needed)
unset ANTHROPIC_API_KEY

export OLLAMA_MAX_LOADED_MODELS=$OLLAMA_MAX_LOADED_MODELS
export OLLAMA_KEEP_ALIVE="$OLLAMA_KEEP_ALIVE"
CONFIG
  fi
  
  echo ""
  echo "✅ Configuration saved to: $AGENTIC_CONF"
  echo ""
  echo "Configuration will be loaded automatically on next shell start."
  echo "To apply now: source ~/.zshrc"
}

function load_agentic_config() {
  if [[ -f "$AGENTIC_CONF" ]]; then
    source "$AGENTIC_CONF"
  else
    # Export defaults (Ollama)
    export ANTHROPIC_AUTH_TOKEN="$AGENTIC_AUTH_TOKEN"
    export ANTHROPIC_BASE_URL="$AGENTIC_BASE_URL"
    export OLLAMA_MAX_LOADED_MODELS
    export OLLAMA_KEEP_ALIVE
  fi
}

# Load config when this module is sourced
load_agentic_config
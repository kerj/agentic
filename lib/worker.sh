#!/bin/bash
# Worker agent — runs either the Claude Code agent or the local Ollama agent.
# Called from worker-once after the worktree is set up.
# Set AGENTIC_LOCAL=1 to use Ollama. Set AGENTIC_LOCAL_MODEL to choose the model.

function run_worker_agent() {
  local request="$1"
  local log_file="${2:-}"   # optional path for JSONL stream
  local model_hint="${3:-}" # optional per-job model override

  # ── Local mode: Ollama ──────────────────────────────────────────────────────
  if [[ "${AGENTIC_LOCAL:-}" == "1" ]]; then
    local model="${model_hint:-${AGENTIC_LOCAL_MODEL:-qwen2.5-coder:32b}}"
    # Use local-specific system prompt tuned for local model limitations
    local local_prompt="$AGENTIC_HOME/agents/worker_local.txt"
    [[ ! -f "$local_prompt" ]] && local_prompt="$AGENTIC_HOME/agents/worker.txt"
    echo "🤖 Running local agent (${model})..."
    echo ""
    if [[ -n "$log_file" ]]; then
      AGENTIC_HOME="$AGENTIC_HOME" \
      AGENTIC_LOCAL_MODEL="$model" \
      AGENTIC_WORKER_PROMPT="$local_prompt" \
        "${AGENTIC_HOME}/venv/bin/python3" "$AGENTIC_HOME/lib/ollama_worker.py" "$request" \
      | tee "$log_file" \
      | "${AGENTIC_HOME}/venv/bin/python3" "$AGENTIC_HOME/lib/stream_parser.py"
    else
      AGENTIC_HOME="$AGENTIC_HOME" \
      AGENTIC_LOCAL_MODEL="$model" \
      AGENTIC_WORKER_PROMPT="$local_prompt" \
        "${AGENTIC_HOME}/venv/bin/python3" "$AGENTIC_HOME/lib/ollama_worker.py" "$request"
    fi
    return $?
  fi

  # ── Cloud mode: Claude Code CLI ─────────────────────────────────────────────
  if ! command -v claude &>/dev/null; then
    echo "❌ Claude Code CLI not found"
    echo "   Install from: https://claude.ai/code"
    return 1
  fi

  local system_prompt
  system_prompt="$(cat "$AGENTIC_HOME/agents/worker.txt")"

  echo "🤖 Running Claude agent..."
  echo ""

  if [[ -n "$log_file" ]]; then
    claude \
      -p "$request" \
      --system-prompt "$system_prompt" \
      --dangerously-skip-permissions \
      --allowedTools "Read,Edit,Write,Bash,Glob,Grep,LS" \
      --output-format stream-json \
      --verbose \
    | tee "$log_file" \
    | "${AGENTIC_HOME}/venv/bin/python3" "$AGENTIC_HOME/lib/stream_parser.py"
  else
    claude \
      -p "$request" \
      --system-prompt "$system_prompt" \
      --dangerously-skip-permissions \
      --allowedTools "Read,Edit,Write,Bash,Glob,Grep,LS"
  fi
}

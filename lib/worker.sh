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

    # Append language-specific section — prefer <profile>-local.txt, fall back to base
    local _local_profile_section="$AGENTIC_HOME/agents/prompt_sections/${AGENTIC_PROFILE:-typescript}-local.txt"
    [[ ! -f "$_local_profile_section" ]] && _local_profile_section="$AGENTIC_HOME/agents/prompt_sections/${AGENTIC_PROFILE:-typescript}.txt"
    [[ ! -f "$_local_profile_section" ]] && _local_profile_section="$AGENTIC_HOME/agents/prompt_sections/typescript-local.txt"
    if [[ -f "$_local_profile_section" ]]; then
      local _local_tmp
      _local_tmp=$(mktemp /tmp/agentic_prompt_XXXXXX)
      if [[ -n "$_local_tmp" ]]; then
        { cat "$local_prompt"; printf '\n\n'; cat "$_local_profile_section"; } > "$_local_tmp"
        local_prompt="$_local_tmp"
      fi
    fi

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

  # Append language-specific build/verify section — cloud gets base file (no -local suffix)
  local _profile_section="$AGENTIC_HOME/agents/prompt_sections/${AGENTIC_PROFILE:-typescript}.txt"
  if [[ ! -f "$_profile_section" ]]; then
    _profile_section="$AGENTIC_HOME/agents/prompt_sections/typescript.txt"
  fi
  if [[ -f "$_profile_section" ]]; then
    system_prompt="${system_prompt}"$'\n\n'"$(cat "$_profile_section")"
  fi

  local model="${model_hint:-${AGENTIC_MODEL:-}}"
  local model_flag=()
  [[ -n "$model" && "$model" != "auto" ]] && model_flag=(--model "$model")

  echo "🤖 Running Claude agent${model:+ ($model)}..."
  echo ""

  if [[ -n "$log_file" ]]; then
    claude \
      -p "$request" \
      --system-prompt "$system_prompt" \
      --dangerously-skip-permissions \
      --allowedTools "Read,Edit,Write,Bash,Glob,Grep,LS" \
      --output-format stream-json \
      --verbose \
      "${model_flag[@]}" \
    | tee "$log_file" \
    | "${AGENTIC_HOME}/venv/bin/python3" "$AGENTIC_HOME/lib/stream_parser.py"
  else
    claude \
      -p "$request" \
      --system-prompt "$system_prompt" \
      --dangerously-skip-permissions \
      --allowedTools "Read,Edit,Write,Bash,Glob,Grep,LS" \
      "${model_flag[@]}"
  fi
}

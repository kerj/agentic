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
    local model="${model_hint:-${AGENTIC_LOCAL_MODEL:-qwen-coder:latest}}"
    # Use local-specific system prompt tuned for local model limitations
    local local_prompt="$AGENTIC_APP/agents/worker_local.txt"
    [[ ! -f "$local_prompt" ]] && local_prompt="$AGENTIC_APP/agents/worker.txt"

    # Append language-specific section — prefer <profile>-local.txt, fall back to base
    local _local_profile_section="$AGENTIC_APP/agents/prompt_sections/${AGENTIC_PROFILE:-typescript}-local.txt"
    [[ ! -f "$_local_profile_section" ]] && _local_profile_section="$AGENTIC_APP/agents/prompt_sections/${AGENTIC_PROFILE:-typescript}.txt"
    [[ ! -f "$_local_profile_section" ]] && _local_profile_section="$AGENTIC_APP/agents/prompt_sections/typescript-local.txt"
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
        "${AGENTIC_PYTHON}" "$AGENTIC_APP/lib/ollama_worker.py" "$request" \
      | tee "$log_file" \
      | "${AGENTIC_PYTHON}" "$AGENTIC_APP/lib/stream_parser.py"
    else
      AGENTIC_HOME="$AGENTIC_HOME" \
      AGENTIC_LOCAL_MODEL="$model" \
      AGENTIC_WORKER_PROMPT="$local_prompt" \
        "${AGENTIC_PYTHON}" "$AGENTIC_APP/lib/ollama_worker.py" "$request"
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
  system_prompt="$(cat "$AGENTIC_APP/agents/worker.txt")"

  # Append language-specific build/verify section — cloud gets base file (no -local suffix)
  local _profile_section="$AGENTIC_APP/agents/prompt_sections/${AGENTIC_PROFILE:-typescript}.txt"
  if [[ ! -f "$_profile_section" ]]; then
    _profile_section="$AGENTIC_APP/agents/prompt_sections/typescript.txt"
  fi
  if [[ -f "$_profile_section" ]]; then
    system_prompt="${system_prompt}"$'\n\n'"$(cat "$_profile_section")"
  fi

  local model="${model_hint:-${AGENTIC_MODEL:-}}"
  local model_flag=()
  [[ -n "$model" && "$model" != "auto" ]] && model_flag=(--model "$model")

  # ROOT GUARD: the claude CLI REFUSES --dangerously-skip-permissions when run as
  # root, then falls into an interactive path that fails as a cryptic
  # "ConnectionRefused" retry loop. The agent worker MUST run unattended (no
  # permission prompts), so running as root is unworkable for cloud jobs. Fail
  # loudly with the real reason instead of the confusing connection error.
  if [[ "$(id -u)" == "0" ]]; then
    echo "❌ Cloud worker is running as ROOT — the Claude CLI blocks"
    echo "   --dangerously-skip-permissions for root, so unattended jobs can't run."
    echo "   Run the container as your host UID:GID (the entrypoint does this via"
    echo "   gosu when HOST_UID/HOST_GID are set). In Docker, ensure HOST_UID/GID"
    echo "   are in docker/.env (the wizard sets them)."
    return 1
  fi

  # CLOUD ENDPOINT SCRUB (defense in depth) — the shared config
  # (lib/config.sh load_agentic_config) historically exported the Ollama
  # backend's ANTHROPIC_BASE_URL=http://localhost:11434 and
  # ANTHROPIC_AUTH_TOKEN=ollama whenever there was no .agentic.conf (the Docker
  # case). Those leaked into THIS cloud path: the claude CLI authenticated fine
  # (apiKeySource: ANTHROPIC_API_KEY) but then dialed localhost:11434 (Ollama's
  # port), where no Anthropic API listens → "Unable to connect to API
  # (ConnectionRefused)" after 10 retries. config.sh now gates that export on
  # local mode, but we still scrub here so a stray operator-set var can't
  # repoison the cloud worker. Clearing both makes the CLI use its real default
  # (api.anthropic.com). Use unset (not ="") — it removes the var unambiguously
  # across CLI versions instead of relying on empty-string handling, and also
  # drops the redundant auth token so there's no second auth signal.
  unset ANTHROPIC_BASE_URL
  unset ANTHROPIC_AUTH_TOKEN

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
    | "${AGENTIC_PYTHON}" "$AGENTIC_APP/lib/stream_parser.py"
  else
    claude \
      -p "$request" \
      --system-prompt "$system_prompt" \
      --dangerously-skip-permissions \
      --allowedTools "Read,Edit,Write,Bash,Glob,Grep,LS" \
      "${model_flag[@]}"
  fi
}

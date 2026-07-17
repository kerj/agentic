#!/bin/bash
# Worker agent — runs either the Claude Code agent or the local Ollama agent.
# Called from worker-once after the worktree is set up.
# Set AGENTIC_LOCAL=1 to use Ollama. Set AGENTIC_LOCAL_MODEL to choose the model.

_detect_pkg_manager() {
  # Priority: lockfile → package.json#packageManager field → npm
  if [[ -f "pnpm-lock.yaml" ]];                         then echo "pnpm"; return; fi
  if [[ -f "yarn.lock" ]];                              then echo "yarn"; return; fi
  if [[ -f "bun.lockb" || -f "bun.lock" ]];            then echo "bun";  return; fi
  if [[ -f "package-lock.json" ]];                      then echo "npm";  return; fi
  if [[ -f "package.json" ]]; then
    local pm
    pm=$(jq -r '.packageManager // ""' package.json 2>/dev/null | cut -d'@' -f1)
    [[ -n "$pm" ]] && echo "$pm" && return
  fi
  echo "npm"
}

_pkg_manager_env_block() {
  local pm="$1"
  local run_cmd add_cmd exec_cmd dlx_cmd
  case "$pm" in
    pnpm) run_cmd="pnpm run"; add_cmd="pnpm add";    exec_cmd="pnpm exec"; dlx_cmd="pnpm dlx" ;;
    yarn) run_cmd="yarn";     add_cmd="yarn add";    exec_cmd="yarn exec"; dlx_cmd="yarn dlx" ;;
    bun)  run_cmd="bun run";  add_cmd="bun add";     exec_cmd="bun x";     dlx_cmd="bunx"     ;;
    *)    run_cmd="npm run";  add_cmd="npm install"; exec_cmd="npx";       dlx_cmd="npx"      ;;
  esac
  cat <<ENV
PROJECT ENVIRONMENT:
Package manager: $pm
Use $pm for ALL package manager operations — do not use npm unless this IS an npm project.
  Install deps:    $pm install
  Run a script:    $run_cmd <script>        ← PREFER for build/lint/test/typecheck
  Run a binary:    $exec_cmd <binary>        ← tsc / eslint / prettier / vitest
  Add a package:   $add_cmd <package>   (dev: $add_cmd -D <package>)
  One-off tool:    $dlx_cmd <binary>        ← ONLY for a tool NOT in this project

RUNNING TOOLS (tsc, eslint, prettier, tests):
- Prefer the package.json script: "$run_cmd build" / "$run_cmd lint" / "$run_cmd typecheck".
  It uses the project's own tool version and config.
- For a bare binary with no script, use "$exec_cmd <binary>" (e.g. "$exec_cmd tsc --noEmit").
  It resolves the project's INSTALLED copy — including in a monorepo/workspace where the
  binary is NOT at the repo-root node_modules/.bin.
- Do NOT run "./node_modules/.bin/<binary>" — in a $pm workspace it often isn't there; use
  "$exec_cmd" instead.
- Do NOT use "$dlx_cmd" for a tool the project depends on — that downloads a DIFFERENT version
  and ignores the project's config.
ENV
}

# Prepare the project toolchain BEFORE the agent runs, for BOTH backends, so the
# agent never has to figure out Node/pnpm/yarn or install deps itself (that
# guesswork is what makes local jobs burn hours + tokens). Best-effort and
# non-fatal: cwd is the job's worktree. Exports PATH so the Node/pm selection
# persists into the agent subprocess (claude CLI or ollama_worker.py).
_prepare_project_toolchain() {
  [[ -f package.json ]] || return 0

  # 1) Node version — honor .nvmrc / .node-version / engines.node via fnm, for
  #    BOTH backends (previously only the local Python worker did this).
  if command -v fnm >/dev/null 2>&1; then
    local pin=""
    if   [[ -f .nvmrc ]];        then pin=$(tr -dc '0-9.' < .nvmrc)
    elif [[ -f .node-version ]]; then pin=$(tr -dc '0-9.' < .node-version)
    else pin=$(jq -r '.engines.node // ""' package.json 2>/dev/null \
               | grep -oE '[0-9]+(\.[0-9]+){0,2}' | head -1); fi
    if [[ -n "$pin" ]]; then
      fnm install "$pin" >/dev/null 2>&1 || true
      local _nbin
      _nbin=$(fnm exec --using="$pin" which node 2>/dev/null || true)
      if [[ -n "$_nbin" ]]; then
        export PATH="$(dirname "$_nbin"):$PATH"
        echo "⬢ Node $pin selected for this project (fnm)"
      else
        echo "⚠️  Project pins Node $pin but fnm could not provide it; using default."
      fi
    fi
  fi

  # 2) Package manager version — honor package.json#packageManager via corepack,
  #    so pnpm@X / yarn@Y is the project's pinned version, not a random one.
  if command -v corepack >/dev/null 2>&1; then
    local _pmspec
    _pmspec=$(jq -r '.packageManager // ""' package.json 2>/dev/null)
    if [[ -n "$_pmspec" && "$_pmspec" != "null" ]]; then
      if corepack prepare "$_pmspec" --activate >/dev/null 2>&1; then
        echo "📦 Package manager $_pmspec activated (corepack)"
      else
        echo "⚠️  corepack could not activate $_pmspec; using the default shim."
      fi
    fi
  fi

  # 3) Install dependencies ONCE, deterministically, for BOTH backends. Reuses
  #    ollama_worker's frozen-install + lockfile-restore logic (single source of
  #    truth) so node_modules is ready before the agent starts and the diff stays
  #    clean. Non-fatal — the agent can still install if this is skipped.
  if [[ -n "${AGENTIC_PYTHON:-}" && -f "${AGENTIC_APP:-}/lib/ollama_worker.py" ]]; then
    "${AGENTIC_PYTHON}" "${AGENTIC_APP}/lib/ollama_worker.py" --prepare-deps 2>&1 || true
  fi
}

function run_worker_agent() {
  local request="$1"
  local log_file="${2:-}"   # optional path for JSONL stream
  local model_hint="${3:-}" # job's model_hint

  # model_hint is now a BACKEND MARKER (local|remote|auto), NOT a model name —
  # the dispatcher routes via AGENTIC_LOCAL and supplies the real model in
  # AGENTIC_LOCAL_MODEL / AGENTIC_MODEL. Using "remote"/"local" as a --model
  # name made the claude CLI 404 ("model 'remote' not found"). So treat those
  # markers as "no override" and fall back to the env-resolved model. (A real
  # model name in model_hint — a legacy/explicit override — is still honored.)
  case "$model_hint" in
    local|remote|auto|"") model_hint="" ;;
  esac

  # ── Prepare the toolchain (Node + pnpm/yarn + deps) BEFORE the agent ──────
  # cwd is the worktree at this point. This runs for BOTH backends so the agent
  # starts with the right Node, the project-pinned package manager, and deps
  # already installed — instead of spending turns/tokens discovering them.
  _prepare_project_toolchain

  # ── Detect package manager (cwd is the worktree at this point) ────────────
  local _pkg_mgr
  _pkg_mgr=$(_detect_pkg_manager)
  local _pkg_env_block
  _pkg_env_block=$(_pkg_manager_env_block "$_pkg_mgr")

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
        { printf '%s\n\n' "$_pkg_env_block"; cat "$local_prompt"; printf '\n\n'; cat "$_local_profile_section"; } > "$_local_tmp"
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
  system_prompt="${_pkg_env_block}"$'\n\n'"$(cat "$AGENTIC_APP/agents/worker.txt")"

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

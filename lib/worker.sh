#!/bin/bash
# Worker agent — runs a Claude Code agent to implement a change in the current directory.
# Called from worker-once after the worktree is set up. The agent reads the project,
# makes changes, verifies with tsc, and commits — no separate pipeline steps needed.

function run_worker_agent() {
  local request="$1"
  local log_file="${2:-}"   # optional path for JSONL stream

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
    # Stream JSON to log file while printing text to stdout for the log panel
    claude \
      -p "$request" \
      --system-prompt "$system_prompt" \
      --dangerously-skip-permissions \
      --allowedTools "Read,Edit,Write,Bash,Glob,Grep,LS" \
      --output-format stream-json \
      --verbose \
    | tee "$log_file" \
    | python3 "$AGENTIC_HOME/lib/stream_parser.py"
  else
    claude \
      -p "$request" \
      --system-prompt "$system_prompt" \
      --dangerously-skip-permissions \
      --allowedTools "Read,Edit,Write,Bash,Glob,Grep,LS"
  fi
}

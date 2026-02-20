#!/bin/bash
# Utility functions — shared across all agentic scripts

# Slugify a string for use in session names
_session_slug() {
  echo "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed 's/[^a-z0-9]/-/g' \
    | sed 's/--*/-/g' \
    | cut -c1-30
}

# Format seconds as "Xm Ys" or "Ys"
format_duration() {
  local secs="${1:-0}"
  local mins=$(( secs / 60 ))
  local rem=$(( secs % 60 ))
  [[ $mins -gt 0 ]] && echo "${mins}m ${rem}s" || echo "${rem}s"
}

# Clean absolute or cwd-relative path prefixes from a file path
_apply_clean_path() {
  local path="$1"
  # Strip cwd prefix
  path="${path#$(pwd)/}"
  # Strip common absolute project path prefixes (e.g. /Users/x/Projects/y/z/)
  path=$(echo "$path" | sed 's|^/[^/]*/[^/]*/[^/]*/[^/]*/||')
  echo "$path"
}

# Resolve active session dir — returns empty string if none found
_apply_resolve_session() {
  if [[ -n "${AGENTIC_SESSION:-}" ]]; then
    echo ".claude/sessions/$AGENTIC_SESSION"
  elif [[ -L ".claude/latest" ]]; then
    AGENTIC_SESSION=$(basename "$(readlink .claude/latest)")
    echo ".claude/latest"
  else
    echo ""
  fi
}

# Get task IDs in execution order from a tasks.json file
_apply_get_task_ids() {
  local tasks_file="$1"
  local task_ids
  task_ids=($(jq -r '.execution_order[]?' "$tasks_file" 2>/dev/null))
  [[ ${#task_ids[@]} -eq 0 ]] && task_ids=($(jq -r '.tasks[]?.id' "$tasks_file"))
  echo "${task_ids[@]}"
}

# Interactive session switcher
function agentic-use() {
  local sessions_dir=".claude/sessions"

  if [[ ! -d "$sessions_dir" ]] || [[ -z "$(ls -A "$sessions_dir" 2>/dev/null)" ]]; then
    echo "❌ No sessions found in $sessions_dir"
    return 1
  fi

  echo "📚 Available sessions:"
  echo ""

  local sessions
  sessions=($(ls -t "$sessions_dir"))
  local i=1

  for session in "${sessions[@]}"; do
    echo "  $i) $session"
    if [[ -f "$sessions_dir/$session/tasks.json" ]]; then
      local task_count
      task_count=$(jq -r '.tasks | length' "$sessions_dir/$session/tasks.json" 2>/dev/null)
      [[ -n "$task_count" && "$task_count" != "null" ]] && echo "     Tasks: $task_count"
    fi
    ((i++))
    echo ""
  done

  read -p "Choose session number: " choice

  if [[ "$choice" -gt 0 && "$choice" -le "${#sessions[@]}" ]] 2>/dev/null; then
    local selected="${sessions[$((choice - 1))]}"

    rm -f .claude/latest
    ln -s "sessions/$selected" .claude/latest
    export AGENTIC_SESSION="$selected"

    echo "✅ Active session: $selected"
    echo ""
    echo "⚠️  To persist in your current shell:"
    echo "    export AGENTIC_SESSION=\"$selected\""
  else
    echo "❌ Invalid choice"
    return 1
  fi
}
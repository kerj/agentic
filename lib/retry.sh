#!/bin/bash
# Session retry/regeneration

function agentic-retry() {
  echo "🔍 Detecting session..."
  echo ""
  
  local session_name=""
  
  # PRIORITY 1: Use .claude/latest symlink
  if [[ -L ".claude/latest" ]]; then
    local latest_target=$(readlink .claude/latest)
    session_name=$(basename "$latest_target")
    echo "✅ Using session from .claude/latest: $session_name"
    export AGENTIC_SESSION="$session_name"
  # PRIORITY 2: Use environment variable
  elif [[ -n "${AGENTIC_SESSION:-}" ]]; then
    session_name="$AGENTIC_SESSION"
    echo "✅ Using session from AGENTIC_SESSION: $session_name"
  else
    echo "❌ No active session"
    echo ""
    echo "No .claude/latest symlink and no AGENTIC_SESSION set."
    echo ""
    echo "Options:"
    echo "  1. Pick a session: agentic use"
    echo "  2. List sessions: agentic list"
    return 1
  fi
  
  echo ""
  
  local session_dir=".claude/sessions/$session_name"
  
  echo "📂 Session directory: $session_dir"
  echo ""
  
  if [[ ! -d "$session_dir" ]]; then
    echo "❌ Session directory doesn't exist!"
    echo ""
    echo "Expected: $session_dir"
    echo "Current directory: $(pwd)"
    echo ""
    
    if [[ -d ".claude/sessions" ]]; then
      echo "Available sessions in this project:"
      ls -1 .claude/sessions/
    else
      echo "No .claude/sessions directory found."
      echo "Are you in the correct project directory?"
    fi
    return 1
  fi
  
  echo "✅ Session directory exists"
  echo ""
  
  if [[ ! -f "$session_dir/request.txt" ]]; then
    echo "❌ Session has no request.txt"
    echo ""
    echo "Session directory contents:"
    ls -la "$session_dir/"
    return 1
  fi
  
  echo "🔄 Retrying session: $session_name"
  echo ""
  echo "Original request:"
  echo "─────────────────────────────────────"
  cat "$session_dir/request.txt"
  echo "─────────────────────────────────────"
  echo ""
  
  if [[ -f "$session_dir/tasks.json" ]]; then
    if jq empty "$session_dir/tasks.json" 2>/dev/null; then
      if ! jq -e '.tasks' "$session_dir/tasks.json" >/dev/null 2>&1; then
        echo "⚠️  tasks.json exists but has invalid structure"
        echo "    Content preview:"
        head -3 "$session_dir/tasks.json" | sed 's/^/    /'
      else
        echo "ℹ️  tasks.json exists and appears valid"
        local task_count=$(jq -r '.tasks | length' "$session_dir/tasks.json" 2>/dev/null)
        echo "   Contains $task_count tasks"
      fi
    else
      echo "⚠️  tasks.json exists but is not valid JSON"
      echo "    Content preview:"
      head -3 "$session_dir/tasks.json" | sed 's/^/    /'
    fi
  else
    echo "⚠️  No tasks.json found"
  fi
  echo ""
  
  local timestamp=$(date +%Y%m%d-%H%M%S)
  local backed_up=false
  
  if [[ -f "$session_dir/tasks.json" ]]; then
    mv "$session_dir/tasks.json" "$session_dir/tasks.json.bad-${timestamp}"
    echo "✅ Backed up tasks.json → tasks.json.bad-${timestamp}"
    backed_up=true
  fi
  
  if [[ -f "$session_dir/validation_issues.txt" ]]; then
    mv "$session_dir/validation_issues.txt" "$session_dir/validation_issues.txt.old-${timestamp}"
    echo "✅ Backed up validation_issues.txt"
    backed_up=true
  fi
  
  if [[ -d "$session_dir/outputs" ]]; then
    mv "$session_dir/outputs" "$session_dir/outputs.bad-${timestamp}"
    echo "✅ Backed up outputs/ → outputs.bad-${timestamp}"
    backed_up=true
  fi
  
  [[ "$backed_up" == true ]] && echo ""
  
  read -p "Regenerate tasks.json? (y/n) " confirm
  if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo "Cancelled"
    return 0
  fi
  
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "Regenerating tasks..."
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  
  # Rebuild context if missing
  local context_file="$session_dir/context.txt"
  if [[ ! -f "$context_file" ]]; then
    echo "📋 Rebuilding context..."
    _archie_build_context "$context_file"
    echo "✅ Context rebuilt"
    echo ""
  else
    echo "✅ Using existing context.txt"
    echo ""
  fi
  
  local architect_prompt
  architect_prompt="$(cat $AGENTIC_HOME/agents/architect.txt)"

  local user_prompt="$(cat "$context_file")

USER REQUEST:
$(cat "$session_dir/request.txt")

Output tasks as valid JSON. Use arrays for multi-line content, not \n.
The 'target' field must be the exact identifier as it appears in source code.
DO NOT wrap output in markdown code fences. Output raw JSON only."

  echo "🏗️  Calling architect..."

  claude_api \
    --model "$AGENTIC_MODEL" \
    --system "$architect_prompt" \
    --cache-system \
    --user "$user_prompt" \
    --output "$session_dir/tasks.json" \
    --usage "$session_dir/retry_${timestamp}_usage.json"

  if [[ $? -ne 0 ]]; then
    echo "❌ API call failed"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "❌ Retry Failed"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "Options:"
    echo "  1. Try again: agentic retry"
    echo "  2. Use a different model: agentic config"
    echo "  3. Start fresh: agentic"
    return 1
  fi

  # Strip markdown fences if present
  if grep -q '```' "$session_dir/tasks.json"; then
    echo "  ⚠️  Cleaning markdown fences from output..."
    local tmp
    tmp=$(mktemp)
    grep -v '^```' "$session_dir/tasks.json" > "$tmp"
    mv "$tmp" "$session_dir/tasks.json"
  fi
  
  if ! jq empty "$session_dir/tasks.json" 2>/dev/null; then
    echo "❌ Invalid JSON generated"
    echo ""
    echo "Raw output:"
    cat "$session_dir/tasks.json"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "❌ Retry Failed"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "Options:"
    echo "  1. Try again: agentic retry"
    echo "  2. Use a different model: agentic config"
    echo "  3. Start fresh: agentic"
    return 1
  fi
  
  if ! jq -e '.tasks' "$session_dir/tasks.json" >/dev/null 2>&1; then
    echo "❌ JSON is valid but missing 'tasks' array"
    echo ""
    echo "Content:"
    jq . "$session_dir/tasks.json"
    echo ""
    echo "Try a different model: agentic config"
    return 1
  fi
  
  echo ""
  echo "✅ Tasks regenerated successfully!"
  echo ""
  echo "📋 New tasks:"
  local task_count=$(jq -r '.tasks | length' "$session_dir/tasks.json")
  echo "  Total: $task_count tasks"
  echo ""
  jq -r '.tasks[]? | "  [\(.id)] \(.action) \(.file) - \(.description // "no description")"' \
    "$session_dir/tasks.json" 2>/dev/null

  if [[ -f "$session_dir/retry_${timestamp}_usage.json" ]]; then
    echo ""
    local input output cache_read
    input=$(jq -r '.input_tokens' "$session_dir/retry_${timestamp}_usage.json")
    output=$(jq -r '.output_tokens' "$session_dir/retry_${timestamp}_usage.json")
    cache_read=$(jq -r '.cache_read_input_tokens' "$session_dir/retry_${timestamp}_usage.json")
    echo "📊 Tokens — input: $input, output: $output, cache read: $cache_read"
  fi
  
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "✅ Retry Complete!"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  echo "Backed up files in: $session_dir/"
  [[ -f "$session_dir/tasks.json.bad-${timestamp}" ]] && echo "  • tasks.json.bad-${timestamp}"
  [[ -d "$session_dir/outputs.bad-${timestamp}" ]] && echo "  • outputs.bad-${timestamp}/"
  echo ""
  echo "Next steps:"
  echo "  1. Review tasks: cat $session_dir/tasks.json"
  echo "  2. Run implement: agentic implement"
  echo "  3. Or full workflow: agentic"
}
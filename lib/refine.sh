#!/bin/bash
# Refine functions

function refine() {
  [[ -f "$AGENTIC_HOME/.agentic.conf" ]] && source "$AGENTIC_HOME/.agentic.conf"

  local session_dir
  session_dir=$(_apply_resolve_session)


  local issues_file="$session_dir/validation_issues.txt"
  if [[ ! -f "$issues_file" ]] || [[ ! -s "$issues_file" ]]; then
    echo "✅ No validation issues found. Nothing to refine."
    echo ""
    echo "If you still want to refine the plan, you can:"
    echo "  1. Manually add issues to: $issues_file"
    echo "  2. Or re-run 'archie' with an updated request"
    return 0
  fi

  echo "🔄 Refining plan based on validation issues..."
  echo ""

  echo "Issues found:"
  cat "$issues_file"
  echo ""

  local original_request=$(cat "$session_dir/request.txt")

  # Get/increment iteration count
  local iteration=1
  if [[ -f "$session_dir/iteration.txt" ]]; then
    iteration=$(cat "$session_dir/iteration.txt")
    ((iteration++))
  fi
  echo "$iteration" > "$session_dir/iteration.txt"
  echo "Iteration: $iteration"
  echo ""

  local refined_request="$original_request

PREVIOUS ATTEMPT HAD THESE ISSUES:
$(cat "$issues_file")

Please create a new plan that avoids these specific issues."

  echo "Refined request:"
  echo "─────────────────────────────────────"
  echo "$refined_request"
  echo "─────────────────────────────────────"
  echo ""

  read -p "Proceed with refined plan? (y/n) " proceed
  [[ ! "$proceed" =~ ^[Yy]$ ]] && echo "Cancelled" && return 1

  echo "$refined_request" > "$session_dir/request.txt"

  # Backup old tasks
  mv "$session_dir/tasks.json" "$session_dir/tasks.json.iteration-$((iteration - 1))"

  echo ""
  echo "🏗️  Re-generating task breakdown..."

  # Rebuild context if missing
  local context_file="$session_dir/context.txt"
  if [[ ! -f "$context_file" ]]; then
    _archie_build_context "$context_file"
  fi

  local architect_prompt="$(cat $AGENTIC_HOME/agents/architect.txt)"
  local user_prompt="$(cat "$context_file")

USER REQUEST (ITERATION $iteration):
$(cat "$session_dir/request.txt")

CRITICAL: Learn from the previous issues listed above. Create a plan that addresses these specific problems.
The 'target' field must be the exact identifier as it appears in source code (e.g. 'getProviderMonthData', not 'getProviderMonthData function').
Output tasks as valid JSON. Use arrays for multi-line content, not \n.
DO NOT wrap output in markdown code fences. Output raw JSON only."

  claude_api \
    --model "$AGENTIC_MODEL" \
    --system "$architect_prompt" \
    --cache-system \
    --user "$user_prompt" \
    --output "$session_dir/tasks.json" \
    --usage "$session_dir/refine_iteration_${iteration}_usage.json"

  if [[ $? -ne 0 ]]; then
    echo "❌ API call failed"
    return 1
  fi

  # Strip markdown fences if present
  if grep -q '```' "$session_dir/tasks.json"; then
    echo "  ⚠️  Cleaning markdown fences from output..."
    sed '/^```json$/d; /^```$/d; /^```/d' "$session_dir/tasks.json" > "$session_dir/tasks.json.tmp" && \
      mv "$session_dir/tasks.json.tmp" "$session_dir/tasks.json"
  fi

  if ! jq empty "$session_dir/tasks.json" 2>/dev/null; then
    echo "❌ Invalid JSON generated"
    return 1
  fi

  echo ""
  echo "📋 New tasks created:"
  local task_count=$(jq -r '.tasks | length' "$session_dir/tasks.json")
  echo "  Total: $task_count tasks"
  echo ""
  jq -r '.tasks[]? | "  [\(.id)] \(.action) \(.file) - \(.description // "no description")"' \
    "$session_dir/tasks.json" 2>/dev/null

  # Show token usage
  if [[ -f "$session_dir/refine_iteration_${iteration}_usage.json" ]]; then
    echo ""
    local input=$(jq -r '.input_tokens' "$session_dir/refine_iteration_${iteration}_usage.json")
    local output=$(jq -r '.output_tokens' "$session_dir/refine_iteration_${iteration}_usage.json")
    local cache_read=$(jq -r '.cache_read_input_tokens' "$session_dir/refine_iteration_${iteration}_usage.json")
    echo "📊 Tokens — input: $input, output: $output, cache read: $cache_read"
  fi

  echo ""

  # Clean old outputs and issues
  rm -rf "$session_dir/outputs"
  rm -f "$issues_file"

  echo "✅ Plan refined"
  echo ""
  if [[ -z "${SKIP_IMPLEMENT_PROMPT:-}" ]]; then
    read -p "Run implement now? (y/n) " run_implement
    if [[ "$run_implement" =~ ^[Yy]$ ]]; then
      implement
    else
      echo "Run 'implement' when ready"
    fi
  fi
}

# refine-with-metrics is now just refine — real token counts always tracked
function refine-with-metrics() {
  refine
}
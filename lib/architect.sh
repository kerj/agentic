#!/bin/bash
# Architect functions

function agentic-list() {
  echo "📚 Available sessions:"
  ls -1t .claude/sessions/ 2>/dev/null | while read session; do
    echo "  $session"
    [[ -f ".claude/sessions/$session/tasks.json" ]] && echo "    ✓ Tasks"
    echo ""
  done
}

function _archie_build_context() {
  local context_file="$1"

  if [[ -f "CLAUDE.md" ]]; then
    echo "=== PROJECT DOCUMENTATION ===" > "$context_file"
    cat CLAUDE.md >> "$context_file"
    echo "" >> "$context_file"
  fi

  echo "=== INSTALLED PACKAGES ===" >> "$context_file"
  if [[ -f "package.json" ]]; then
    jq -r '.dependencies // {} | keys[]' package.json 2>/dev/null >> "$context_file"
    jq -r '.devDependencies // {} | keys[]' package.json 2>/dev/null >> "$context_file"
  fi
  echo "" >> "$context_file"

  echo "=== EXISTING SOURCE FILES ===" >> "$context_file"
  find . -type f \( -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.jsx" \) \
    -not -path "*/node_modules/*" \
    -not -path "*/.git/*" \
    -not -path "*/dist/*" \
    -not -path "*/build/*" \
    -not -path "*/.next/*" \
    -not -path "*/coverage/*" \
    -not -name "*.test.*" \
    -not -name "*.spec.*" \
    2>/dev/null | sort >> "$context_file"
  echo "" >> "$context_file"

  echo "=== EXISTING TEST FILES (for reference - shows test location pattern) ===" >> "$context_file"
  find . -type f \( -name "*.test.ts" -o -name "*.test.tsx" -o -name "*.spec.ts" -o -name "*.spec.tsx" \) \
    -not -path "*/node_modules/*" \
    -not -path "*/.git/*" \
    2>/dev/null | head -10 >> "$context_file"
  echo "" >> "$context_file"
}

function archie() {
  local timestamp=$(date +%Y%m%d-%H%M%S)
  read -p "What should I build? " user_message

  [[ -z "$user_message" ]] && echo "Cancelled" && return 1

  local slug=$(_session_slug "$user_message")
  export AGENTIC_SESSION="${timestamp}_${slug}"
  local session_dir=".claude/sessions/$AGENTIC_SESSION"
  mkdir -p "$session_dir"

  echo "$user_message" > "$session_dir/request.txt"
  echo "📁 Session: $AGENTIC_SESSION"

  if [[ ! -f "CLAUDE.md" ]]; then
    echo "⚠️  No CLAUDE.md found"
    read -p "Create one now? (y/n) " create_doc
    if [[ "$create_doc" =~ ^[Yy]$ ]]; then
      agentic-doc-gen
    else
      echo "Proceeding without project documentation (not recommended)"
    fi
  else
    echo "📖 Reading CLAUDE.md..."
  fi

  echo "🔍 Analyzing project..."

  local context_file="$session_dir/context.txt"
  _archie_build_context "$context_file"

  local architect_prompt="$(cat $AGENTIC_HOME/agents/architect.txt)"
  local user_prompt="$(cat "$context_file")

USER REQUEST:
$(cat "$session_dir/request.txt")

Output tasks as valid JSON. Use arrays for multi-line content, not \n.
The 'target' field must be the exact identifier as it appears in source code (e.g. 'getProviderMonthData', not 'getProviderMonthData function').
DO NOT wrap output in markdown code fences. Output raw JSON only."

  echo "🏗️  Generating task breakdown..."

  claude_api \
    --model "$AGENTIC_MODEL" \
    --system "$architect_prompt" \
    --cache-system \
    --user "$user_prompt" \
    --output "$session_dir/tasks.json" \
    --usage "$session_dir/architect_usage.json"

  local api_result=$?

  if [[ $api_result -ne 0 ]]; then
    echo "❌ API call failed"
    return 1
  fi

  # Strip markdown fences if present
  if grep -q '```' "$session_dir/tasks.json"; then
    echo "  ⚠️  Cleaning markdown fences from output..."
    sed '/^```json$/d; /^```$/d; /^```/d' "$session_dir/tasks.json" > "$session_dir/tasks.json.tmp" && \
      mv "$session_dir/tasks.json.tmp" "$session_dir/tasks.json"
  fi

  # Validate JSON
  if ! jq empty "$session_dir/tasks.json" 2>/dev/null; then
    echo "❌ Invalid JSON generated"
    echo ""
    echo "Raw output:"
    cat "$session_dir/tasks.json"
    return 1
  fi

  # Display tasks
  echo ""
  echo "📋 Tasks created:"
  local task_count=$(jq -r '.tasks | length' "$session_dir/tasks.json")
  echo "  Total: $task_count tasks"
  echo ""
  jq -r '.tasks[]? | "  [\(.id)] \(.action) \(.file) - \(.description // "no description")"' \
    "$session_dir/tasks.json" 2>/dev/null

  # Show real token usage
  if [[ -f "$session_dir/architect_usage.json" ]]; then
    echo ""
    local input=$(jq -r '.input_tokens' "$session_dir/architect_usage.json")
    local output=$(jq -r '.output_tokens' "$session_dir/architect_usage.json")
    local cache_read=$(jq -r '.cache_read_input_tokens' "$session_dir/architect_usage.json")
    local cache_write=$(jq -r '.cache_creation_input_tokens' "$session_dir/architect_usage.json")
    echo "📊 Tokens — input: $input, output: $output, cache write: $cache_write, cache read: $cache_read"
  fi

  echo ""
  echo "✅ Tasks saved to $session_dir/tasks.json"

  rm -f .claude/latest
  ln -s "sessions/$AGENTIC_SESSION" .claude/latest

  echo ""
  echo "Next: Review tasks.json then run 'implement'"
}

# archie-with-metrics is now just archie — real token counts always tracked
function archie-with-metrics() {
  archie
}
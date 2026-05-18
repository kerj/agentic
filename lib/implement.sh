#!/bin/bash
# Implementor functions
# ─────────────────────────────────────────────────────────────────────────────
# Stitching helpers
# ─────────────────────────────────────────────────────────────────────────────
_find_last_import_line() {
  local file="$1"

  local last_start
  last_start=$(grep -n "^import " "$file" | tail -1 | cut -d: -f1)
  [[ -z "$last_start" ]] && echo "" && return

  # Single-line import: ends on the same line as `from '...'`
  # Handles both semicolon and no-semicolon codebases.
  if sed -n "${last_start}p" "$file" | grep -qE "from ['\"][^'\"]*['\"]"; then
    echo "$last_start"
    return
  fi

  # Multi-line import — scan forward to the closing `from '...'` line
  local total_lines
  total_lines=$(wc -l < "$file" | tr -d ' ')
  local n=$((last_start + 1))
  while [[ $n -le $total_lines ]]; do
    if sed -n "${n}p" "$file" | grep -qE "from ['\"][^'\"]*['\"]"; then
      echo "$n"
      return
    fi
    ((n++))
  done

  echo "$last_start"
}

_find_function_range() {
  local file="$1"
  local target="$2"

  local start_line
  start_line=$(grep -n \
    -E "(export )?(default )?(async )?function[[:space:]]+${target}[[:space:](<]|(export )?(const|let|var)[[:space:]]+${target}[[:space:]]*[=:]|(export )?(abstract )?class[[:space:]]+${target}([[:space:]{<(]|$)|^[[:space:]]+(async[[:space:]]+|static[[:space:]]+|private[[:space:]]+|public[[:space:]]+|protected[[:space:]]+|override[[:space:]]+|abstract[[:space:]]+)*${target}[[:space:]]*[(<]|^[[:space:]]+(private[[:space:]]+|public[[:space:]]+|protected[[:space:]]+|static[[:space:]]+|readonly[[:space:]]+)*${target}[[:space:]]*=[[:space:]]*(async[[:space:]]+)?\(" \
    "$file" | head -1 | cut -d: -f1)

  # Test framework blocks: describe/it/test/beforeEach/afterEach where target is the description string
  if [[ -z "$start_line" ]]; then
    start_line=$(grep -n \
      -E "(describe|it|test|beforeEach|afterEach|beforeAll|afterAll)[[:space:]]*\([[:space:]]*['\"]${target}['\"]" \
      "$file" | head -1 | cut -d: -f1)
  fi

  if [[ -z "$start_line" ]]; then
    echo ""
    return 1
  fi

  local depth=0
  local end_line=0
  local found_open=false
  local line_num=0

  while IFS= read -r line; do
    ((line_num++))
    [[ $line_num -lt $start_line ]] && continue

    local stripped
    stripped=$(echo "$line" | sed "s/\`[^\`]*\`//g; s/\\\${[^}]*}//g; s/\"[^\"]*\"//g; s/'[^']*'//g")

    local opens closes
    opens=$(echo "$stripped" | tr -cd '{' | wc -c | tr -d ' ')
    closes=$(echo "$stripped" | tr -cd '}' | wc -c | tr -d ' ')
    depth=$((depth + opens - closes))

    [[ $opens -gt 0 ]] && found_open=true

    if [[ "$found_open" == true && $depth -le 0 ]]; then
      end_line=$line_num
      break
    fi
  done < "$file"

  if [[ $end_line -eq 0 ]]; then
    echo ""
    return 1
  fi

  echo "${start_line}:${end_line}"
}

_stitch_insert_after() {
  local original="$1"
  local patch="$2"
  local after_line="$3"
  local output="$4"

  {
    head -n "$after_line" "$original"
    cat "$patch"
    tail -n "+$((after_line + 1))" "$original"
  } > "$output"
}

_stitch_append() {
  local original="$1"
  local patch="$2"
  local output="$3"

  {
    cat "$original"
    echo ""
    cat "$patch"
  } > "$output"
}

_stitch_replace_range() {
  local original="$1"
  local patch="$2"
  local start_line="$3"
  local end_line="$4"
  local output="$5"

  {
    [[ $start_line -gt 1 ]] && head -n "$((start_line - 1))" "$original"
    cat "$patch"
    tail -n "+$((end_line + 1))" "$original"
  } > "$output"
}

_stitch_delete_range() {
  local original="$1"
  local start_line="$2"
  local end_line="$3"
  local output="$4"

  {
    [[ $start_line -gt 1 ]] && head -n "$((start_line - 1))" "$original"
    tail -n "+$((end_line + 1))" "$original"
  } > "$output"
}

_find_end_of_imports() {
  local file="$1"
  local last_import
  last_import=$(_find_last_import_line "$file")
  echo "${last_import:-0}"
}

# ─────────────────────────────────────────────────────────────────────────────
# Source file discovery for test tasks
# ─────────────────────────────────────────────────────────────────────────────

_find_source_for_test() {
  local clean_task_file="$1"
  local task_desc="$2"
  local task_json="$3"
  local tasks_file="$4"
  local session_dir="$5"

  # Strategy 0: Extract path from task description
  if [[ -n "$task_desc" ]]; then
    local desc_file
    desc_file=$(echo "$task_desc" | grep -oE '(src|app|lib|utils|components|controllers|services)/[a-zA-Z0-9/_-]+\.(ts|tsx|js|jsx)' | head -1)
    if [[ -n "$desc_file" ]]; then
      if [[ -f "$desc_file" ]]; then
        echo "$desc_file"
        return
      else
        local deps
        deps=$(echo "$task_json" | jq -r '.dependencies[]?' 2>/dev/null)
        for dep in $deps; do
          local dep_file
          dep_file=$(jq -r ".tasks[]? | select(.id == \"$dep\") | .file" "$tasks_file")
          if [[ "$dep_file" == "$desc_file" && -f "$session_dir/outputs/task_${dep}.txt" ]]; then
            echo "$session_dir/outputs/task_${dep}.txt"
            return
          fi
        done
      fi
    fi
  fi

  # Strategy 1: src/ path in description
  local source_file
  source_file=$(echo "$task_desc" | grep -oE 'src/[^ ,]+' | head -1)
  if [[ -n "$source_file" && -f "$source_file" ]]; then
    echo "$source_file"
    return
  fi

  # Strategy 2: Derive from test filename
  local base_name
  base_name=$(basename "$clean_task_file")
  base_name="${base_name%.test.ts}" base_name="${base_name%.test.tsx}"
  base_name="${base_name%.spec.ts}" base_name="${base_name%.spec.tsx}"
  base_name="${base_name%.test.js}" base_name="${base_name%.test.jsx}"
  local test_dir
  test_dir=$(dirname "$clean_task_file")

  for pattern in \
    "${test_dir}/${base_name}.ts" \
    "${test_dir}/${base_name}.tsx" \
    "${test_dir}/${base_name}.js" \
    "${test_dir}/${base_name}.jsx" \
    "$(echo "$test_dir" | sed 's/test/src/g')/${base_name}.ts" \
    "$(echo "$test_dir" | sed 's/test/src/g')/${base_name}.tsx" \
    "$(echo "$test_dir" | sed 's/__tests__//g')/${base_name}.ts" \
    "src/${base_name}.ts" \
    "src/${base_name}.tsx" \
    "app/${base_name}.ts" \
    "app/${base_name}.tsx" \
    "src/controllers/${base_name}.ts" \
    "src/components/${base_name}.tsx" \
    "src/utils/${base_name}.ts" \
    "app/utils/${base_name}.ts"
  do
    if [[ -f "$pattern" ]]; then
      echo "$pattern"
      return
    fi
  done

  # Strategy 3: Project-wide search (respects .llmignore)
  find . -type f \( -name "${base_name}.ts" -o -name "${base_name}.tsx" \
    -o -name "${base_name}.js" -o -name "${base_name}.jsx" \) \
    -not -path "*/node_modules/*" -not -path "*/.git/*" \
    -not -path "*/test/*" -not -path "*/tests/*" -not -path "*/__tests__/*" \
    -not -name "*.test.*" -not -name "*.spec.*" \
    2>/dev/null | _llmignore_filter | head -1
}

# ─────────────────────────────────────────────────────────────────────────────
# Relevant section extractor (fallback for unrecognised types)
# ─────────────────────────────────────────────────────────────────────────────

extract_relevant_section() {
  local file="$1"
  local target="$2"
  local file_size="$3"

  if [[ -n "$target" && "$target" != "null" ]]; then
    local start_line
    start_line=$(grep -n "$target" "$file" | head -1 | cut -d: -f1)
    if [[ -n "$start_line" ]]; then
      local from=$(( start_line > 20 ? start_line - 20 : 1 ))
      local to=$(( start_line + 60 ))
      [[ $to -gt $file_size ]] && to=$file_size
      echo "PARTIAL FILE — lines $from-$to of $file_size:"
      echo "LINE_RANGE=${from}:${to}"
      echo '```'
      sed -n "${from},${to}p" "$file"
      echo '```'
      return
    fi
  fi

  local head_end=60
  local tail_start=$(( file_size - 20 ))
  echo "PARTIAL FILE — structure ($file_size lines total):"
  echo "LINE_RANGE=1:${file_size}"
  echo '```'
  head -n "$head_end" "$file"
  echo "... [$(( file_size - head_end - 20 )) lines omitted] ..."
  tail -n 20 "$file"
  echo '```'
}

# ─────────────────────────────────────────────────────────────────────────────
# Main implement function
# ─────────────────────────────────────────────────────────────────────────────

function implement() {
  [[ -f "$AGENTIC_HOME/.agentic.conf" ]] && source "$AGENTIC_HOME/.agentic.conf"

  local session_dir
  session_dir=$(_apply_resolve_session)

  local tasks_file="$session_dir/tasks.json"
  [[ ! -f "$tasks_file" ]] && echo "❌ No tasks found. Run 'archie' first." && return 1

  echo "📁 Session: $AGENTIC_SESSION"

  local project_doc=""
  if [[ -f "CLAUDE.md" ]]; then
    echo "📖 Reading CLAUDE.md..."
    project_doc="$(cat CLAUDE.md)"
  fi

  # Build cached system prompt — static across all tasks in this session
  local implementor_prompt
  implementor_prompt="$(cat $AGENTIC_HOME/agents/implementor.txt)"
  local system_prompt="$implementor_prompt"
  if [[ -n "$project_doc" ]]; then
    system_prompt="$implementor_prompt

PROJECT DOCUMENTATION:
$project_doc"
  fi

  if ! command -v jq &> /dev/null; then
    echo "❌ jq required"
    return 1
  fi

  local task_ids
  task_ids=($(jq -r '.execution_order[]?' "$tasks_file" 2>/dev/null))
  [[ ${#task_ids[@]} -eq 0 ]] && task_ids=($(jq -r '.tasks[]?.id' "$tasks_file"))

  local total=${#task_ids[@]}
  local completed=0
  local failed=0

  mkdir -p "$session_dir/outputs"

  echo "🔨 Executing $total tasks..."
  echo ""

  for task_id in "${task_ids[@]}"; do
    ((completed++))

    local task_json
    task_json=$(jq ".tasks[]? | select(.id == \"$task_id\")" "$tasks_file")

    local task_file task_action task_desc target modification_type
    task_file=$(echo "$task_json" | jq -r '.file')
    task_action=$(echo "$task_json" | jq -r '.action')
    task_desc=$(echo "$task_json" | jq -r '.description // "no description"')
    target=$(echo "$task_json" | jq -r '.target // ""')
    modification_type=$(echo "$task_json" | jq -r '.modification_type // "full_file"')

    echo "[$completed/$total] Task $task_id: $task_desc"
    echo "  File: $task_file ($task_action / $modification_type)"
    [[ -n "$target" && "$target" != "null" ]] && echo "  Target: $target"

    local clean_task_file="${task_file#$(pwd)/}"

    # Resolve the effective base file for this task.
    # If a prior task in this session already produced output for the same path,
    # use that output as the base for context and stitching — otherwise we'd
    # overwrite the prior task's changes when apply writes sequentially.
    local _file_key="${clean_task_file//\//__}"
    local _ptr="$session_dir/latest_output_${_file_key}"
    local effective_base="$clean_task_file"
    if [[ -f "$_ptr" ]]; then
      local _prior_output
      _prior_output=$(cat "$_ptr")
      if [[ -f "$_prior_output" ]]; then
        effective_base="$_prior_output"
        echo "  🔗 Building on prior task output for $(basename "$clean_task_file")"
      fi
    fi
    # True if we have any readable version of this file (original or prior output)
    local base_exists=false
    [[ -f "$effective_base" ]] && base_exists=true

    local raw_output="$session_dir/outputs/task_${task_id}.txt"

    # ── DELETE — no model call ───────────────────────────────────────────────
    if [[ "$task_action" == "DELETE" || "$modification_type" == "delete_code" ]]; then
      if [[ "$base_exists" == true && -n "$target" && "$target" != "null" ]]; then
        local range
        range=$(_find_function_range "$effective_base" "$target")
        if [[ -n "$range" ]]; then
          local del_start del_end
          del_start=$(echo "$range" | cut -d: -f1)
          del_end=$(echo "$range" | cut -d: -f2)
          local stitched="$session_dir/outputs/task_${task_id}_stitched.txt"
          _stitch_delete_range "$effective_base" "$del_start" "$del_end" "$stitched"
          mv "$stitched" "$raw_output"
          echo "$raw_output" > "$_ptr"
          echo "  ✅ Deleted '$target' (lines $del_start-$del_end)"
        else
          echo "  ⚠️  Could not locate '$target' for deletion"
          ((failed++))
        fi
      else
        touch "$raw_output"
        echo "  ✅ File deletion marked"
      fi
      echo ""
      sleep "${AGENTIC_TASK_DELAY:-0.3}"
      continue
    fi

    # ── Build file context ───────────────────────────────────────────────────
    local existing_content=""
    local stored_range=""

    if [[ "$modification_type" != "full_file" && "$base_exists" == true ]]; then
      local file_size
      file_size=$(wc -l < "$effective_base" | tr -d ' ')

      case "$modification_type" in
        add_import)
          existing_content="EXISTING FILE — imports section (first 30 lines):
\`\`\`
$(head -30 "$effective_base")
\`\`\`"
          ;;

        add_function|add_export)
          existing_content="EXISTING FILE — end of file (last 20 lines, $file_size total):
\`\`\`
$(tail -20 "$effective_base")
\`\`\`"
          ;;

        add_type)
          local last_import
          last_import=$(_find_last_import_line "$effective_base")
          local show_from=1
          local show_to=$(( ${last_import:-0} + 10 ))
          [[ $show_to -gt $file_size ]] && show_to=$file_size
          existing_content="EXISTING FILE — after imports (lines $show_from-$show_to of $file_size):
\`\`\`
$(sed -n "${show_from},${show_to}p" "$effective_base")
\`\`\`"
          ;;

        modify_function|add_to_function|add_hook|wrap_component)
          if [[ -n "$target" && "$target" != "null" ]]; then
            local range
            range=$(_find_function_range "$effective_base" "$target")
            if [[ -n "$range" ]]; then
              local fn_start fn_end
              fn_start=$(echo "$range" | cut -d: -f1)
              fn_end=$(echo "$range" | cut -d: -f2)
              stored_range="${fn_start}:${fn_end}"
              echo "  📍 Found '$target' at lines $fn_start-$fn_end"
              existing_content="TARGET FUNCTION — '$target' (lines $fn_start-$fn_end of $file_size):
LINE_RANGE=${fn_start}:${fn_end}
\`\`\`
$(sed -n "${fn_start},${fn_end}p" "$effective_base")
\`\`\`

IMPORTS (for context):
\`\`\`
$(head -20 "$effective_base")
\`\`\`"
            else
              echo "  ⚠️  Could not locate '$target' — falling back to full file"
              modification_type="full_file"
              if [[ $file_size -le 200 ]]; then
                existing_content="EXISTING FILE ($file_size lines):
\`\`\`
$(cat "$effective_base")
\`\`\`"
              else
                local section_output
                section_output=$(extract_relevant_section "$effective_base" "$target" "$file_size")
                existing_content="$section_output"
                stored_range=$(echo "$section_output" | grep '^LINE_RANGE=' | cut -d= -f2)
              fi
            fi
          fi
          ;;

        add_route)
          if [[ $file_size -le 150 ]]; then
            existing_content="EXISTING FILE ($file_size lines):
\`\`\`
$(cat "$effective_base")
\`\`\`"
          else
            local section_output
            section_output=$(extract_relevant_section "$effective_base" "$target" "$file_size")
            existing_content="$section_output"
          fi
          ;;
      esac
    fi

    # ── Source context for test files ────────────────────────────────────────
    local source_context=""
    if [[ "$clean_task_file" =~ \.test\.(ts|tsx|js|jsx)$ ]] || \
       [[ "$clean_task_file" =~ \.spec\.(ts|tsx|js|jsx)$ ]]; then
      echo "  🧪 Test file — locating source..."
      local source_file
      source_file=$(_find_source_for_test \
        "$clean_task_file" "$task_desc" "$task_json" "$tasks_file" "$session_dir")

      if [[ -n "$source_file" && -f "$source_file" ]]; then
        echo "     ✓ Source: $source_file"
        source_context="
SOURCE CODE BEING TESTED ($source_file):
\`\`\`typescript
$(cat "$source_file")
\`\`\`
CRITICAL: Test the ACTUAL functions above. Use exact names and signatures."
      else
        echo "     ⚠️  Source not found"
        source_context="
WARNING: Source file not found for: $task_desc
Use TODO comments for assertions until source is available."
      fi
    fi

    # ── Dependency outputs ───────────────────────────────────────────────────
    local dep_context=""
    local deps
    deps=$(echo "$task_json" | jq -r '.dependencies[]?' 2>/dev/null)
    for dep in $deps; do
      if [[ -f "$session_dir/outputs/task_${dep}.txt" ]]; then
        dep_context+="
OUTPUT FROM TASK $dep:
$(cat "$session_dir/outputs/task_${dep}.txt")
"
      fi
    done

    # ── Execute instruction per modification_type ────────────────────────────
    local execute_instruction
    case "$modification_type" in
      full_file)
        execute_instruction="Output the COMPLETE file content. No explanations, no markdown fences."
        ;;
      add_import)
        execute_instruction="Output ONLY the single import line to add. No explanations, no markdown fences."
        ;;
      add_function|add_type|add_export)
        execute_instruction="Output ONLY the new declaration. No explanations, no markdown fences."
        ;;
      modify_function|add_to_function|add_hook|wrap_component)
        execute_instruction="Output ONLY the complete replacement function shown above (lines $stored_range), with the required change applied. Preserve everything outside the function exactly. No explanations, no markdown fences."
        ;;
      add_route)
        execute_instruction="Output ONLY the single route line to add. No explanations, no markdown fences."
        ;;
      *)
        execute_instruction="Output ONLY the required code. No explanations, no markdown fences."
        ;;
    esac

    local user_prompt="TASK TO EXECUTE:
$task_json

$existing_content
$source_context
$dep_context

EXECUTE THE TASK:
$execute_instruction"

    # ── API call ─────────────────────────────────────────────────────────────
    claude_api \
      --model "$AGENTIC_MODEL" \
      --system "$system_prompt" \
      --cache-system \
      --temperature  0.2 \
      --user "$user_prompt" \
      --output "$raw_output" \
      --usage "$session_dir/outputs/task_${task_id}_usage.json"

    if [[ $? -ne 0 || ! -s "$raw_output" ]]; then
      echo "  ❌ API call failed or empty output"
      ((failed++))
      echo ""
      continue
    fi

    # Clean stray markdown fences
    if grep -q '```' "$raw_output"; then
      sed -i.bak '/^```/d' "$raw_output"
      rm -f "${raw_output}.bak"
    fi

    local output_lines
    output_lines=$(wc -l < "$raw_output" | tr -d ' ')
    echo "  ✅ Generated ($output_lines lines)"

    # ── Stitching ────────────────────────────────────────────────────────────
    if [[ "$task_action" != "CREATE" && "$base_exists" == true && \
          "$modification_type" != "full_file" ]]; then

      local stitched="$session_dir/outputs/task_${task_id}_stitched.txt"
      local stitch_ok=false

      case "$modification_type" in

        add_import)
          local last_import
          last_import=$(_find_last_import_line "$effective_base")
          if [[ -n "$last_import" && "$last_import" -gt 0 ]]; then
            _stitch_insert_after "$effective_base" "$raw_output" "$last_import" "$stitched"
            echo "  🔧 Import inserted after line $last_import"
          else
            { cat "$raw_output"; echo ""; cat "$effective_base"; } > "$stitched"
            echo "  🔧 Import prepended (no existing imports)"
          fi
          stitch_ok=true
          ;;

        add_function|add_export)
          _stitch_append "$effective_base" "$raw_output" "$stitched"
          echo "  🔧 Appended to end of file"
          stitch_ok=true
          ;;

        add_type)
          local after_line
          after_line=$(_find_end_of_imports "$effective_base")
          if [[ "$after_line" -gt 0 ]]; then
            _stitch_insert_after "$effective_base" "$raw_output" "$after_line" "$stitched"
            echo "  🔧 Type inserted after imports (line $after_line)"
          else
            _stitch_append "$effective_base" "$raw_output" "$stitched"
            echo "  🔧 Type appended to end of file"
          fi
          stitch_ok=true
          ;;

        modify_function|add_to_function|add_hook|wrap_component)
          if [[ -n "$stored_range" ]]; then
            local fn_start fn_end
            fn_start=$(echo "$stored_range" | cut -d: -f1)
            fn_end=$(echo "$stored_range" | cut -d: -f2)
            _stitch_replace_range "$effective_base" "$raw_output" "$fn_start" "$fn_end" "$stitched"
            echo "  🔧 Function replaced (lines $fn_start-$fn_end)"
            stitch_ok=true
          else
            echo "  ⚠️  No range stored — writing output as full file"
            cp "$raw_output" "$stitched"
            stitch_ok=true
          fi
          ;;

        add_route)
          local router_close_line
          router_close_line=$(grep -n '</Routes>\|</Switch>\|</Router>' "$effective_base" \
            | tail -1 | cut -d: -f1)
          if [[ -n "$router_close_line" ]]; then
            _stitch_insert_after "$effective_base" "$raw_output" \
              "$((router_close_line - 1))" "$stitched"
            echo "  🔧 Route inserted before closing tag (line $router_close_line)"
          else
            _stitch_append "$effective_base" "$raw_output" "$stitched"
            echo "  🔧 Route appended (no closing router tag found)"
          fi
          stitch_ok=true
          ;;

      esac

      if [[ "$stitch_ok" == true && -f "$stitched" ]]; then
        local original_lines stitched_lines
        original_lines=$(wc -l < "$effective_base" | tr -d ' ')
        stitched_lines=$(wc -l < "$stitched" | tr -d ' ')
        local shrink=$(( stitched_lines - original_lines ))
        if [[ $shrink -lt -30 ]]; then
          echo "  ⚠️  Output is $((shrink * -1)) lines shorter than original — review before applying"
        fi
        # Preserve the pre-stitch model output so validate() can inspect
        # what the model actually produced, not the stitched full file.
        cp "$raw_output" "$session_dir/outputs/task_${task_id}_raw.txt"
        mv "$stitched" "$raw_output"
      fi
    fi

    # Track this task's output as the latest for its file path so subsequent
    # tasks targeting the same file stitch against it, not the original on disk.
    [[ -s "$raw_output" ]] && echo "$raw_output" > "$_ptr"

    # Token usage
    if [[ -f "$session_dir/outputs/task_${task_id}_usage.json" ]]; then
      local cache_read input_tok
      cache_read=$(jq -r '.cache_read_input_tokens' "$session_dir/outputs/task_${task_id}_usage.json")
      input_tok=$(jq -r '.input_tokens' "$session_dir/outputs/task_${task_id}_usage.json")
      echo "  📊 Tokens — input: $input_tok, cache read: $cache_read"
    fi

    echo ""
    sleep "${AGENTIC_TASK_DELAY:-0.3}"
  done

  # ── Summary ───────────────────────────────────────────────────────────────
  echo "─────────────────────────────────────"
  echo "✅ Completed: $((total - failed))/$total"
  [[ $failed -gt 0 ]] && echo "❌ Failed: $failed"
  echo ""

  echo "🔍 Running validation..."
  echo ""
  validate
  local validation_result=$?

  if [[ $validation_result -eq 0 ]]; then
    echo ""
    echo "✅ All checks passed!"
    echo ""
    if [[ -z "${SKIP_APPLY_PROMPT:-}" ]]; then
      read -p "Apply these changes? (y/n) " do_apply
      [[ "$do_apply" =~ ^[Yy]$ ]] && apply || echo "Run 'apply' when ready"
    else
      echo "Ready for apply step..."
    fi
  else
    echo ""
    echo "❌ Validation failed"
    echo ""
    if [[ -n "${SKIP_APPLY_PROMPT:-}" ]]; then
      return 1
    fi
    echo "Options:"
    echo "  1. Run 'refine' to improve the plan and try again"
    echo "  2. Run 'apply' anyway (not recommended)"
    echo "  3. Manually fix issues in .claude/latest/outputs/"
  fi
}

function implement-with-metrics() {
  export SKIP_APPLY_PROMPT=1
  local result
  implement
  result=$?
  unset SKIP_APPLY_PROMPT
  return $result
}
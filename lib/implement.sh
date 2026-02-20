#!/bin/bash
# Implementor functions
# ─────────────────────────────────────────────────────────────────────────────
# Stitching helpers
# ─────────────────────────────────────────────────────────────────────────────
_find_last_import_line() {
  local file="$1"
  grep -n "^import " "$file" | tail -1 | cut -d: -f1
}

_find_function_range() {
  local file="$1"
  local target="$2"

  local start_line
  start_line=$(grep -n \
    -E "(export )?(default )?(async )?function[[:space:]]+${target}[[:space:](]|(export )?(const|let|var)[[:space:]]+${target}[[:space:]]*=|(export )?class[[:space:]]+${target}[[:space:]{(]" \
    "$file" | head -1 | cut -d: -f1)

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
    stripped=$(echo "$line" | sed "s/\"[^\"]*\"//g; s/'[^']*'//g")

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

  # Strategy 3: Project-wide search
  find . -type f \( -name "${base_name}.ts" -o -name "${base_name}.tsx" \
    -o -name "${base_name}.js" -o -name "${base_name}.jsx" \) \
    -not -path "*/node_modules/*" -not -path "*/.git/*" \
    -not -path "*/test/*" -not -path "*/tests/*" -not -path "*/__tests__/*" \
    -not -name "*.test.*" -not -name "*.spec.*" | head -1
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

    local clean_task_file="${task_file#/Users/*/Projects/*/projects/*/}"
    clean_task_file="${clean_task_file#$(pwd)/}"

    local raw_output="$session_dir/outputs/task_${task_id}.txt"

    # ── DELETE — no model call ───────────────────────────────────────────────
    if [[ "$task_action" == "DELETE" || "$modification_type" == "delete_code" ]]; then
      if [[ -f "$clean_task_file" && -n "$target" && "$target" != "null" ]]; then
        local range
        range=$(_find_function_range "$clean_task_file" "$target")
        if [[ -n "$range" ]]; then
          local del_start del_end
          del_start=$(echo "$range" | cut -d: -f1)
          del_end=$(echo "$range" | cut -d: -f2)
          local stitched="$session_dir/outputs/task_${task_id}_stitched.txt"
          _stitch_delete_range "$clean_task_file" "$del_start" "$del_end" "$stitched"
          mv "$stitched" "$raw_output"
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
      sleep 0.3
      continue
    fi

    # ── Build file context ───────────────────────────────────────────────────
    local existing_content=""
    local stored_range=""

    if [[ "$modification_type" != "full_file" && -f "$clean_task_file" ]]; then
      local file_size
      file_size=$(wc -l < "$clean_task_file" | tr -d ' ')

      case "$modification_type" in
        add_import)
          existing_content="EXISTING FILE — imports section (first 30 lines):
\`\`\`
$(head -30 "$clean_task_file")
\`\`\`"
          ;;

        add_function|add_export)
          existing_content="EXISTING FILE — end of file (last 20 lines, $file_size total):
\`\`\`
$(tail -20 "$clean_task_file")
\`\`\`"
          ;;

        add_type)
          local last_import
          last_import=$(_find_last_import_line "$clean_task_file")
          local show_from=1
          local show_to=$(( ${last_import:-0} + 10 ))
          [[ $show_to -gt $file_size ]] && show_to=$file_size
          existing_content="EXISTING FILE — after imports (lines $show_from-$show_to of $file_size):
\`\`\`
$(sed -n "${show_from},${show_to}p" "$clean_task_file")
\`\`\`"
          ;;

        modify_function|add_to_function|add_hook|wrap_component)
          if [[ -n "$target" && "$target" != "null" ]]; then
            local range
            range=$(_find_function_range "$clean_task_file" "$target")
            if [[ -n "$range" ]]; then
              local fn_start fn_end
              fn_start=$(echo "$range" | cut -d: -f1)
              fn_end=$(echo "$range" | cut -d: -f2)
              stored_range="${fn_start}:${fn_end}"
              echo "  📍 Found '$target' at lines $fn_start-$fn_end"
              existing_content="TARGET FUNCTION — '$target' (lines $fn_start-$fn_end of $file_size):
LINE_RANGE=${fn_start}:${fn_end}
\`\`\`
$(sed -n "${fn_start},${fn_end}p" "$clean_task_file")
\`\`\`

IMPORTS (for context):
\`\`\`
$(head -20 "$clean_task_file")
\`\`\`"
            else
              echo "  ⚠️  Could not locate '$target' — falling back to full file"
              modification_type="full_file"
              if [[ $file_size -le 200 ]]; then
                existing_content="EXISTING FILE ($file_size lines):
\`\`\`
$(cat "$clean_task_file")
\`\`\`"
              else
                local section_output
                section_output=$(extract_relevant_section "$clean_task_file" "$target" "$file_size")
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
$(cat "$clean_task_file")
\`\`\`"
          else
            local section_output
            section_output=$(extract_relevant_section "$clean_task_file" "$target" "$file_size")
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
    if [[ "$task_action" != "CREATE" && -f "$clean_task_file" && \
          "$modification_type" != "full_file" ]]; then

      local stitched="$session_dir/outputs/task_${task_id}_stitched.txt"
      local stitch_ok=false

      case "$modification_type" in

        add_import)
          local last_import
          last_import=$(_find_last_import_line "$clean_task_file")
          if [[ -n "$last_import" && "$last_import" -gt 0 ]]; then
            _stitch_insert_after "$clean_task_file" "$raw_output" "$last_import" "$stitched"
            echo "  🔧 Import inserted after line $last_import"
          else
            { cat "$raw_output"; echo ""; cat "$clean_task_file"; } > "$stitched"
            echo "  🔧 Import prepended (no existing imports)"
          fi
          stitch_ok=true
          ;;

        add_function|add_export)
          _stitch_append "$clean_task_file" "$raw_output" "$stitched"
          echo "  🔧 Appended to end of file"
          stitch_ok=true
          ;;

        add_type)
          local after_line
          after_line=$(_find_end_of_imports "$clean_task_file")
          if [[ "$after_line" -gt 0 ]]; then
            _stitch_insert_after "$clean_task_file" "$raw_output" "$after_line" "$stitched"
            echo "  🔧 Type inserted after imports (line $after_line)"
          else
            _stitch_append "$clean_task_file" "$raw_output" "$stitched"
            echo "  🔧 Type appended to end of file"
          fi
          stitch_ok=true
          ;;

        modify_function|add_to_function|add_hook|wrap_component)
          if [[ -n "$stored_range" ]]; then
            local fn_start fn_end
            fn_start=$(echo "$stored_range" | cut -d: -f1)
            fn_end=$(echo "$stored_range" | cut -d: -f2)
            _stitch_replace_range "$clean_task_file" "$raw_output" "$fn_start" "$fn_end" "$stitched"
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
          router_close_line=$(grep -n '</Routes>\|</Switch>\|</Router>' "$clean_task_file" \
            | tail -1 | cut -d: -f1)
          if [[ -n "$router_close_line" ]]; then
            _stitch_insert_after "$clean_task_file" "$raw_output" \
              "$((router_close_line - 1))" "$stitched"
            echo "  🔧 Route inserted before closing tag (line $router_close_line)"
          else
            _stitch_append "$clean_task_file" "$raw_output" "$stitched"
            echo "  🔧 Route appended (no closing router tag found)"
          fi
          stitch_ok=true
          ;;

      esac

      if [[ "$stitch_ok" == true && -f "$stitched" ]]; then
        local original_lines stitched_lines
        original_lines=$(wc -l < "$clean_task_file" | tr -d ' ')
        stitched_lines=$(wc -l < "$stitched" | tr -d ' ')
        local shrink=$(( stitched_lines - original_lines ))
        if [[ $shrink -lt -30 ]]; then
          echo "  ⚠️  Output is $((shrink * -1)) lines shorter than original — review before applying"
        fi
        mv "$stitched" "$raw_output"
      fi
    fi

    # Token usage
    if [[ -f "$session_dir/outputs/task_${task_id}_usage.json" ]]; then
      local cache_read input_tok
      cache_read=$(jq -r '.cache_read_input_tokens' "$session_dir/outputs/task_${task_id}_usage.json")
      input_tok=$(jq -r '.input_tokens' "$session_dir/outputs/task_${task_id}_usage.json")
      echo "  📊 Tokens — input: $input_tok, cache read: $cache_read"
    fi

    echo ""
    sleep 0.3
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
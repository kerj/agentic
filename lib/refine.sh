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

  # Preserve a pristine copy of the user's original request on first refine,
  # then always refine from THAT — not from the previously-refined request.
  # Otherwise iteration N's request contains N-1 stacked issue lists.
  if [[ ! -f "$session_dir/original_request.txt" ]]; then
    cp "$session_dir/request.txt" "$session_dir/original_request.txt"
  fi
  local original_request=$(cat "$session_dir/original_request.txt")

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

  # Restore test preference from the original session
  local test_directive=""
  if [[ -f "$session_dir/include_tests.txt" ]]; then
    local _include_tests
    _include_tests=$(cat "$session_dir/include_tests.txt")
    if [[ ! "$_include_tests" =~ ^[Yy]$ ]]; then
      test_directive="
SKIP TESTS: Do NOT create any test file tasks. Implementation tasks only."
    fi
  fi

  local architect_prompt="$(cat $AGENTIC_HOME/agents/architect.txt)"
  local user_prompt="$(cat "$context_file")

USER REQUEST (ITERATION $iteration):
$(cat "$session_dir/request.txt")
${test_directive}
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

# ─────────────────────────────────────────────────────────────────────────────
# _repair_cascade — after repairing task REPAIRED_ID for file CLEAN_PATH,
# walk forward through execution_order and re-stitch any later task that
# targets the same file against the updated output chain.
#
# This fixes the case where a failing task A (e.g. add_import) is followed by
# a passing task B (e.g. modify_function) on the same file. Without cascade,
# apply writes B's stale output last — losing A's repair. With cascade, B's
# output is re-stitched against A's repaired result so the final file is correct.
# ─────────────────────────────────────────────────────────────────────────────
_repair_cascade() {
  local session_dir="$1"
  local tasks_file="$2"
  local repaired_task_id="$3"
  local clean_path="$4"
  local cascade_base="$5"   # repaired task's output — starting point for chain
  local repair_ptr="$6"     # path to the _repair_ptr file (updated as chain grows)

  local all_ids
  all_ids=($(jq -r '.execution_order[]?' "$tasks_file" 2>/dev/null))
  [[ ${#all_ids[@]} -eq 0 ]] && \
    all_ids=($(jq -r '.tasks[]?.id' "$tasks_file" 2>/dev/null))

  local past_repaired=false

  for cascade_id in "${all_ids[@]}"; do
    if [[ "$cascade_id" == "$repaired_task_id" ]]; then
      past_repaired=true
      continue
    fi
    [[ "$past_repaired" == false ]] && continue

    local c_file
    c_file=$(jq -r ".tasks[]? | select(.id == \"$cascade_id\") | .file" \
      "$tasks_file" 2>/dev/null)
    local clean_c="${c_file#$(pwd)/}"
    [[ "$clean_c" != "$clean_path" ]] && continue

    local c_output="$session_dir/outputs/task_${cascade_id}.txt"
    local c_raw="$session_dir/outputs/task_${cascade_id}_raw.txt"

    # Full-file task: output already contains the complete file.
    # No re-stitch possible, but track it as the new cascade_base.
    if [[ ! -f "$c_raw" ]]; then
      [[ -f "$c_output" ]] && cascade_base="$c_output"
      continue
    fi

    local c_json; c_json=$(jq ".tasks[]? | select(.id == \"$cascade_id\")" "$tasks_file")
    local c_action; c_action=$(echo "$c_json" | jq -r '.action')
    local c_mod;    c_mod=$(echo "$c_json"    | jq -r '.modification_type // "full_file"')
    local c_target; c_target=$(echo "$c_json" | jq -r '.target // ""')

    [[ "$c_action" == "CREATE" || "$c_mod" == "full_file" ]] && {
      cascade_base="$c_output"
      continue
    }

    local cascade_stitched="$session_dir/outputs/task_${cascade_id}_cascade.txt"
    local ok=false

    case "$c_mod" in
      add_import)
        local li; li=$(_find_last_import_line "$cascade_base")
        if [[ -n "$li" && "$li" -gt 0 ]]; then
          _stitch_insert_after "$cascade_base" "$c_raw" "$li" "$cascade_stitched"
        else
          { cat "$c_raw"; echo ""; cat "$cascade_base"; } > "$cascade_stitched"
        fi
        ok=true
        ;;
      add_function|add_export)
        _stitch_append "$cascade_base" "$c_raw" "$cascade_stitched"
        ok=true
        ;;
      add_type)
        local al; al=$(_find_end_of_imports "$cascade_base")
        if [[ "$al" -gt 0 ]]; then
          _stitch_insert_after "$cascade_base" "$c_raw" "$al" "$cascade_stitched"
        else
          _stitch_append "$cascade_base" "$c_raw" "$cascade_stitched"
        fi
        ok=true
        ;;
      modify_function|add_to_function|add_hook|wrap_component)
        if [[ -n "$c_target" && "$c_target" != "null" ]]; then
          local rng; rng=$(_find_function_range "$cascade_base" "$c_target")
          if [[ -n "$rng" ]]; then
            local rs re
            rs=$(echo "$rng" | cut -d: -f1)
            re=$(echo "$rng" | cut -d: -f2)
            _stitch_replace_range "$cascade_base" "$c_raw" "$rs" "$re" "$cascade_stitched"
            ok=true
          else
            echo "   ⚠️  Cascade: '$c_target' not found in updated file — task $cascade_id skipped"
          fi
        fi
        ;;
      add_route)
        local rcl
        rcl=$(grep -n '</Routes>\|</Switch>\|</Router>' "$cascade_base" \
          | tail -1 | cut -d: -f1)
        if [[ -n "$rcl" ]]; then
          _stitch_insert_after "$cascade_base" "$c_raw" "$((rcl - 1))" "$cascade_stitched"
        else
          _stitch_append "$cascade_base" "$c_raw" "$cascade_stitched"
        fi
        ok=true
        ;;
    esac

    if [[ "$ok" == true && -f "$cascade_stitched" ]]; then
      mv "$cascade_stitched" "$c_output"
      cascade_base="$c_output"
      echo "$cascade_base" > "$repair_ptr"
      echo "   🔗 Cascaded re-stitch → task $cascade_id"
    fi
  done
}

# ─────────────────────────────────────────────────────────────────────────────
# repair — fix failing task outputs in place WITHOUT regenerating the plan.
#
# refine() throws away tasks.json and the entire outputs/ directory and asks
# the architect to replan from scratch. That's the right move when the plan
# itself is wrong, but it's massive overkill when the plan is fine and one
# or two tasks just produced bad code.
#
# repair() does the targeted thing: parse validation_issues.txt for the
# "Task NNN:" prefixes that validate() writes, and for each affected task
# re-prompt the model with (original task spec + current candidate output +
# the specific issues against it). Tasks not mentioned in the issues file
# are left completely alone.
# ─────────────────────────────────────────────────────────────────────────────
function repair() {
  [[ -f "$AGENTIC_HOME/.agentic.conf" ]] && source "$AGENTIC_HOME/.agentic.conf"

  local session_dir
  session_dir=$(_apply_resolve_session)

  if [[ -z "${AGENTIC_SESSION:-}" ]]; then
    if [[ -L ".claude/latest" ]]; then
      export AGENTIC_SESSION=$(basename "$(readlink .claude/latest)")
    fi
  fi

  local issues_file="$session_dir/validation_issues.txt"
  if [[ ! -f "$issues_file" ]] || [[ ! -s "$issues_file" ]]; then
    echo "✅ No validation issues found. Nothing to repair."
    return 0
  fi

  local tasks_file="$session_dir/tasks.json"
  if [[ ! -f "$tasks_file" ]]; then
    echo "❌ No tasks.json — cannot repair without the plan."
    return 1
  fi

  if ! command -v jq &> /dev/null; then
    echo "❌ jq required"
    return 1
  fi

  # ── Parse failing task IDs out of the issues file ────────────────────────
  # Two formats are possible:
  #   1. validate() format:  "Task 003 (src/foo.ts): modify_function ..."
  #   2. review() format:    "1. **File:** `src/foo.ts` **~Line:** 5"
  # Try the Task-prefix format first; fall back to file-path lookup against
  # tasks.json if that finds nothing.
  #
  # The `|| true` on each grep is critical: grep returns 1 when it finds
  # no matches, and under `set -e` that silently kills the orchestrator
  # before any error message gets printed.
  local failing_ids
  failing_ids=$(grep -oE '^Task [a-zA-Z0-9_-]+' "$issues_file" 2>/dev/null \
    | awk '{print $2}' \
    | sort -u || true)

  if [[ -z "$failing_ids" ]]; then
    echo "ℹ️  No 'Task NNN:' prefixes found — trying file-path lookup (review format)..."

    # Extract file paths from markdown-style review output:
    #   **File:** `src/foo.ts`         → src/foo.ts
    #   **File:** src/foo.ts           → src/foo.ts
    # Also catch bare `path/to/file.ts` mentions.
    local mentioned_files
    mentioned_files=$(grep -oE '\*\*File:\*\*[[:space:]]*`?[a-zA-Z0-9_./-]+`?' "$issues_file" 2>/dev/null \
      | sed -E 's/\*\*File:\*\*[[:space:]]*`?([a-zA-Z0-9_./-]+)`?/\1/' \
      | sort -u || true)

    if [[ -z "$mentioned_files" ]]; then
      mentioned_files=$(grep -oE '`[a-zA-Z0-9_/-]+\.(ts|tsx|js|jsx|json)`' "$issues_file" 2>/dev/null \
        | tr -d '`' \
        | sort -u || true)
    fi

    if [[ -z "$mentioned_files" ]]; then
      echo "⚠️  Could not parse any task identifiers or file paths from $issues_file"
      echo "    Issues file content:"
      sed 's/^/      /' "$issues_file"
      echo ""
      echo "    Either fix the issues manually or run 'refine' to replan."
      return 1
    fi

    echo "   Found referenced files:"
    echo "$mentioned_files" | sed 's/^/     • /'
    echo ""

    # Resolve each file path to its task ID via tasks.json
    local resolved_ids=""
    while IFS= read -r mf; do
      [[ -z "$mf" ]] && continue
      local matched
      matched=$(jq -r --arg f "$mf" \
        '.tasks[]? | select(.file == $f or (.file | endswith($f))) | .id' \
        "$tasks_file" 2>/dev/null || true)
      if [[ -n "$matched" ]]; then
        resolved_ids+="$matched"$'\n'
      else
        echo "   ⚠️  No task in tasks.json owns '$mf' — skipping"
      fi
    done <<< "$mentioned_files"

    failing_ids=$(echo "$resolved_ids" | grep -v '^$' | sort -u || true)

    if [[ -z "$failing_ids" ]]; then
      echo "❌ None of the mentioned files map to tasks in tasks.json."
      echo "   Run 'refine' to replan from scratch, or fix manually."
      return 1
    fi
  fi

  local fail_count
  fail_count=$(echo "$failing_ids" | wc -l | tr -d ' ')
  echo "🔧 Repairing $fail_count failing task(s) in place..."
  echo "   (Tasks not listed below will not be touched.)"
  echo ""

  # Track repair iteration so we don't overwrite history
  local repair_iter=1
  if [[ -f "$session_dir/repair_iteration.txt" ]]; then
    repair_iter=$(cat "$session_dir/repair_iteration.txt")
    ((repair_iter++))
  fi
  echo "$repair_iter" > "$session_dir/repair_iteration.txt"

  # Build the cached system prompt (same one implement uses)
  local project_doc=""
  [[ -f "CLAUDE.md" ]] && project_doc="$(cat CLAUDE.md)"
  local implementor_prompt
  implementor_prompt="$(cat "$AGENTIC_HOME/agents/implementor.txt")"
  local system_prompt="$implementor_prompt"
  if [[ -n "$project_doc" ]]; then
    system_prompt="$implementor_prompt

PROJECT DOCUMENTATION:
$project_doc"
  fi

  local repaired=0
  local still_failing=0

  for task_id in $failing_ids; do
    local task_json
    task_json=$(jq ".tasks[]? | select(.id == \"$task_id\")" "$tasks_file")

    if [[ -z "$task_json" ]]; then
      echo "  ⚠️  Task $task_id referenced in issues but not in tasks.json — skipping"
      ((still_failing++))
      continue
    fi

    local task_file task_action task_desc target modification_type
    task_file=$(echo "$task_json" | jq -r '.file')
    task_action=$(echo "$task_json" | jq -r '.action')
    task_desc=$(echo "$task_json" | jq -r '.description // "no description"')
    target=$(echo "$task_json" | jq -r '.target // ""')
    modification_type=$(echo "$task_json" | jq -r '.modification_type // "full_file"')

    echo "🔨 Task $task_id: $task_desc"
    echo "   File: $task_file ($modification_type)"

    # The issues that mention THIS task. Try Task-prefix format first
    # (validate output), fall back to lines mentioning the task's file
    # (review output).
    local task_issues
    task_issues=$(grep "^Task $task_id" "$issues_file" 2>/dev/null || true)
    if [[ -z "$task_issues" ]]; then
      task_issues=$(grep -F "$task_file" "$issues_file" 2>/dev/null || true)
    fi
    if [[ -z "$task_issues" ]]; then
      task_issues="(no specific issue text found — generic repair pass)"
    fi
    echo "   Issues:"
    echo "$task_issues" | sed 's/^/     • /'

    local raw_output="$session_dir/outputs/task_${task_id}.txt"
    local raw_model_output="$session_dir/outputs/task_${task_id}_raw.txt"

    if [[ ! -f "$raw_output" ]]; then
      echo "   ❌ No existing output to repair (missing $raw_output)"
      ((still_failing++))
      echo ""
      continue
    fi

    # Snapshot the current candidate before overwriting
    cp "$raw_output" "$session_dir/outputs/task_${task_id}.before-repair-${repair_iter}.txt"
    [[ -f "$raw_model_output" ]] && \
      cp "$raw_model_output" "$session_dir/outputs/task_${task_id}_raw.before-repair-${repair_iter}.txt"

    # The candidate to show the model: prefer the raw pre-stitch output
    # for partial modifications, fall back to the stitched/full output.
    local candidate_for_prompt="$raw_output"
    [[ -f "$raw_model_output" ]] && candidate_for_prompt="$raw_model_output"
    local candidate_content
    candidate_content=$(cat "$candidate_for_prompt")

    # Per-modification-type instruction — same shape as implement() so the
    # model returns the right slice (full file vs single function vs single line)
    local execute_instruction
    case "$modification_type" in
      full_file)
        execute_instruction="Output the COMPLETE corrected file content. No explanations, no markdown fences."
        ;;
      add_import)
        execute_instruction="Output ONLY the single corrected import line. No explanations, no markdown fences."
        ;;
      add_function|add_type|add_export)
        execute_instruction="Output ONLY the corrected new declaration. No explanations, no markdown fences."
        ;;
      modify_function|add_to_function|add_hook|wrap_component)
        execute_instruction="Output ONLY the complete corrected replacement function. Do NOT include imports or any other code outside the function. No explanations, no markdown fences."
        ;;
      add_route)
        execute_instruction="Output ONLY the single corrected route line. No explanations, no markdown fences."
        ;;
      *)
        execute_instruction="Output ONLY the corrected code in the same shape as the previous attempt. No explanations, no markdown fences."
        ;;
    esac

    local user_prompt="TASK SPEC:
$task_json

YOUR PREVIOUS ATTEMPT AT THIS TASK:
\`\`\`
$candidate_content
\`\`\`

VALIDATION ISSUES WITH YOUR PREVIOUS ATTEMPT:
$task_issues

Your job is to fix the specific issues listed above and return a corrected version of the SAME slice of code your previous attempt was trying to produce. Do not change the shape of the output — if the previous attempt was a single function, return a single function; if it was one import line, return one import line.

$execute_instruction"

    local repaired_output="$session_dir/outputs/task_${task_id}.repaired.txt"

    claude_api \
      --model "$AGENTIC_MODEL" \
      --system "$system_prompt" \
      --cache-system \
      --temperature 0.2 \
      --user "$user_prompt" \
      --output "$repaired_output" \
      --usage "$session_dir/outputs/task_${task_id}_repair_${repair_iter}_usage.json"

    if [[ $? -ne 0 || ! -s "$repaired_output" ]]; then
      echo "   ❌ API call failed or empty output"
      rm -f "$repaired_output"
      ((still_failing++))
      echo ""
      continue
    fi

    # Strip stray fences
    if grep -q '```' "$repaired_output"; then
      sed -i.bak '/^```/d' "$repaired_output"
      rm -f "${repaired_output}.bak"
    fi

    # ── Re-stitch if this was a partial modification ─────────────────────────
    local clean_task_file="${task_file#$(pwd)/}"

    # Use the same effective-base logic as implement(): if a prior repair in
    # this pass already produced output for this file, stitch against that
    # rather than the original on disk — otherwise the second repair on the
    # same file clobbers the first.
    local _file_key="${clean_task_file//\//__}"
    local _repair_ptr="$session_dir/repair_latest_${_file_key}"
    local stitch_base="$clean_task_file"
    if [[ -f "$_repair_ptr" ]]; then
      local _prior; _prior=$(cat "$_repair_ptr")
      [[ -f "$_prior" ]] && stitch_base="$_prior"
    fi

    local needs_stitch=false
    if [[ "$task_action" != "CREATE" && \
          "$modification_type" != "full_file" && \
          -f "$stitch_base" ]]; then
      needs_stitch=true
    fi

    if [[ "$needs_stitch" == false ]]; then
      # Full file or CREATE — repaired output IS the new candidate
      cp "$repaired_output" "$raw_output"
      [[ -f "$raw_model_output" ]] && cp "$repaired_output" "$raw_model_output"
      rm -f "$repaired_output"
      echo "$raw_output" > "$_repair_ptr"
      _repair_cascade "$session_dir" "$tasks_file" \
        "$task_id" "$clean_task_file" "$raw_output" "$_repair_ptr"
      echo "   ✅ Repaired (full file)"
      ((repaired++))
      echo ""
      continue
    fi

    # Partial modification — re-locate the target in the stitch base and
    # splice the repaired slice in.
    local stitched="$session_dir/outputs/task_${task_id}_repair_stitched.txt"
    local stitch_ok=false

    case "$modification_type" in
      add_import)
        local last_import
        last_import=$(_find_last_import_line "$stitch_base")
        if [[ -n "$last_import" && "$last_import" -gt 0 ]]; then
          _stitch_insert_after "$stitch_base" "$repaired_output" "$last_import" "$stitched"
        else
          { cat "$repaired_output"; echo ""; cat "$stitch_base"; } > "$stitched"
        fi
        stitch_ok=true
        ;;

      add_function|add_export)
        _stitch_append "$stitch_base" "$repaired_output" "$stitched"
        stitch_ok=true
        ;;

      add_type)
        local after_line
        after_line=$(_find_end_of_imports "$stitch_base")
        if [[ "$after_line" -gt 0 ]]; then
          _stitch_insert_after "$stitch_base" "$repaired_output" "$after_line" "$stitched"
        else
          _stitch_append "$stitch_base" "$repaired_output" "$stitched"
        fi
        stitch_ok=true
        ;;

      modify_function|add_to_function|add_hook|wrap_component)
        if [[ -n "$target" && "$target" != "null" ]]; then
          local range
          range=$(_find_function_range "$stitch_base" "$target")
          if [[ -n "$range" ]]; then
            local fn_start fn_end
            fn_start=$(echo "$range" | cut -d: -f1)
            fn_end=$(echo "$range" | cut -d: -f2)
            _stitch_replace_range "$stitch_base" "$repaired_output" "$fn_start" "$fn_end" "$stitched"
            stitch_ok=true
          else
            echo "   ⚠️  Could not relocate '$target' in $stitch_base"
          fi
        fi
        ;;

      add_route)
        local router_close_line
        router_close_line=$(grep -n '</Routes>\|</Switch>\|</Router>' "$stitch_base" \
          | tail -1 | cut -d: -f1)
        if [[ -n "$router_close_line" ]]; then
          _stitch_insert_after "$stitch_base" "$repaired_output" \
            "$((router_close_line - 1))" "$stitched"
        else
          _stitch_append "$stitch_base" "$repaired_output" "$stitched"
        fi
        stitch_ok=true
        ;;
    esac

    if [[ "$stitch_ok" == true && -f "$stitched" ]]; then
      # Save the new pre-stitch raw output for validate() to inspect
      cp "$repaired_output" "$raw_model_output"
      mv "$stitched" "$raw_output"
      rm -f "$repaired_output"
      echo "$raw_output" > "$_repair_ptr"
      _repair_cascade "$session_dir" "$tasks_file" \
        "$task_id" "$clean_task_file" "$raw_output" "$_repair_ptr"
      echo "   ✅ Repaired and re-stitched"
      ((repaired++))
    else
      echo "   ❌ Stitching failed — leaving previous candidate in place"
      rm -f "$repaired_output"
      ((still_failing++))
    fi
    echo ""
  done

  echo "─────────────────────────────────────"
  echo "✅ Repaired: $repaired"
  [[ $still_failing -gt 0 ]] && echo "❌ Still failing: $still_failing"
  echo ""

  # Re-run validation against the repaired outputs
  echo "🔍 Re-running validation..."
  echo ""
  validate
  local validation_result=$?

  if [[ $validation_result -eq 0 ]]; then
    echo ""
    echo "✅ All checks passed after repair"
    echo ""
    if [[ -z "${SKIP_APPLY_PROMPT:-}" ]]; then
      read -p "Apply these changes? (y/n) " do_apply
      [[ "$do_apply" =~ ^[Yy]$ ]] && apply || echo "Run 'apply' when ready"
    fi
    return 0
  else
    echo ""
    echo "⚠️  Some issues remain after repair."
    # Inside the orchestrator, core.sh owns the next-step decision —
    # don't print our own remediation menu.
    if [[ -z "${SKIP_APPLY_PROMPT:-}" ]]; then
      echo "   Options:"
      echo "     1. Run 'repair' again to take another pass"
      echo "     2. Run 'refine' to throw out the plan and replan from scratch"
      echo "     3. Manually fix remaining issues in $session_dir/outputs/"
    fi
    return 1
  fi
}

# refine-with-metrics is now just refine — real token counts always tracked
function refine-with-metrics() {
  refine
}
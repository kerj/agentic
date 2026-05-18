#!/bin/bash
# Validate functions

function validate() {
  [[ -f "$AGENTIC_HOME/.agentic.conf" ]] && source "$AGENTIC_HOME/.agentic.conf"

  local session_dir
  session_dir=$(_apply_resolve_session)
  if [[ -z "$session_dir" ]]; then
    echo "❌ No active session."
    return 1
  fi

  echo "🔍 Validating session: $AGENTIC_SESSION"
  echo ""

  local tasks_file="$session_dir/tasks.json"
  local task_ids
  task_ids=($(_apply_get_task_ids "$tasks_file"))

  local issues=()
  local warnings=()

  for task_id in "${task_ids[@]}"; do
    local task_json
    task_json=$(jq ".tasks[]? | select(.id == \"$task_id\")" "$tasks_file")

    local task_file task_action task_desc modification_type target
    task_file=$(echo "$task_json" | jq -r '.file')
    task_action=$(echo "$task_json" | jq -r '.action')
    task_desc=$(echo "$task_json" | jq -r '.description // "no description"')
    modification_type=$(echo "$task_json" | jq -r '.modification_type // "full_file"')
    target=$(echo "$task_json" | jq -r '.target // ""')

    task_file=$(_apply_clean_path "$task_file")

    local output_file="$session_dir/outputs/task_${task_id}.txt"
    local raw_model_output="$session_dir/outputs/task_${task_id}_raw.txt"
    local check_file="$output_file"
    [[ -f "$raw_model_output" ]] && check_file="$raw_model_output"

    echo "Task $task_id ($modification_type): $task_desc"

    if [[ "$modification_type" == "delete_code" ]]; then
      if [[ -f "$output_file" && -s "$output_file" ]]; then
        echo "  ✅ Deletion stitched"
      elif [[ "$task_action" == "DELETE" && ! -f "$output_file" ]]; then
        echo "  ✅ Full file deletion marked"
      else
        echo "  ⚠️  delete_code output missing or empty"
        warnings+=("Task $task_id: delete_code output missing")
      fi
      echo ""
      continue
    fi

    if [[ ! -f "$output_file" ]]; then
      echo "  ❌ No output generated"
      issues+=("Task $task_id: No output file generated")
      echo ""
      continue
    fi

    if [[ ! -s "$output_file" ]]; then
      echo "  ❌ Output is empty"
      issues+=("Task $task_id: Output file is empty")
      echo ""
      continue
    fi

    local output_lines
    output_lines=$(wc -l < "$output_file" | tr -d ' ')

    if grep -q '```' "$check_file"; then
      echo "  ⚠️  Contains markdown fences"
      warnings+=("Task $task_id: Output contains markdown fences")
    fi

    if grep -qiE \
      '^\s*\.\.\.$|\.\.\..*rest.*code|\.\.\..*omitted|\.\.\..*more|// your code here|// implement|/\* implement|// placeholder|// add implementation' \
      "$check_file"; then
      echo "  ❌ Contains placeholder text — output is incomplete"
      issues+=("Task $task_id ($task_file): Output contains placeholder text or TODO markers")
    fi

    if [[ "$task_action" != "DELETE" && ! "$task_file" =~ \. ]]; then
      echo "  ❌ File path has no extension: $task_file"
      issues+=("Task $task_id: File path '$task_file' has no extension")
    fi

    case "$modification_type" in
      add_import)
        local shape_lines=$output_lines
        [[ -f "$raw_model_output" ]] && shape_lines=$(wc -l < "$raw_model_output" | tr -d ' ')
        if [[ $shape_lines -gt 8 ]]; then
          echo "  ⚠️  add_import output is $shape_lines lines — expected a single import statement"
          warnings+=("Task $task_id: add_import should be one import statement, got $shape_lines lines")
        else
          echo "  ✅ Import shape correct ($shape_lines line(s))"
        fi

        # Duplicate import check.
        # NOTE: every grep below has `|| true` because grep returns 1 on
        # no-match, and pipefail propagates that to the assignment, which
        # set -e will then use to silently kill the entire script.
        if [[ -f "$task_file" ]]; then
          local import_symbol=""
          import_symbol=$(grep -oE '\{[^}]+\}' "$check_file" 2>/dev/null | head -1 | tr -d '{ }' || true)
          if [[ -n "$import_symbol" ]] && grep -q "$import_symbol" "$task_file" 2>/dev/null; then
            echo "  ⚠️  '$import_symbol' may already be imported in $task_file"
            warnings+=("Task $task_id: '$import_symbol' may already be imported — could cause duplicate")
          fi
        fi
        ;;

      add_route)
        local route_lines=$output_lines
        [[ -f "$raw_model_output" ]] && route_lines=$(wc -l < "$raw_model_output" | tr -d ' ')
        if [[ $route_lines -gt 6 ]]; then
          echo "  ⚠️  add_route output is $route_lines lines — expected a single <Route /> element"
          warnings+=("Task $task_id: add_route should be a single Route element, got $route_lines lines")
        else
          echo "  ✅ Route shape correct ($route_lines line(s))"
        fi
        ;;

      add_export)
        local export_lines=$output_lines
        [[ -f "$raw_model_output" ]] && export_lines=$(wc -l < "$raw_model_output" | tr -d ' ')
        if [[ $export_lines -gt 5 ]]; then
          echo "  ⚠️  add_export seems large ($export_lines lines)"
          warnings+=("Task $task_id: add_export seems large at $export_lines lines")
        fi
        ;;

      modify_function|add_to_function|add_hook|wrap_component)
        if [[ -f "$raw_model_output" ]] && grep -qE "^import " "$raw_model_output" 2>/dev/null; then
          echo "  ❌ $modification_type output contains import statements — model output full file instead of just the function"
          issues+=("Task $task_id ($task_file): $modification_type output contains imports — stitching will corrupt the file")
        fi

        if [[ -n "$target" && "$target" != "null" ]]; then
          if ! grep -q "$target" "$check_file" 2>/dev/null; then
            echo "  ⚠️  Target '$target' not found in output — model may have rewritten wrong function"
            warnings+=("Task $task_id: Target '$target' not found in output")
          fi
        fi
        ;;
    esac

    if [[ "$task_action" == "MODIFY" && \
          "$modification_type" == "full_file" && \
          -f "$task_file" ]]; then
      local orig_lines
      orig_lines=$(wc -l < "$task_file" | tr -d ' ')
      local shrink=$(( output_lines - orig_lines ))
      if [[ $shrink -lt -30 ]]; then
        echo "  ⚠️  Output is $((shrink * -1)) lines shorter than original — may be truncated"
        warnings+=("Task $task_id ($task_file): Output is $((shrink * -1)) lines shorter than original")
      fi
    fi

    case "$task_file" in
      *.json)
        if jq empty "$output_file" 2>/dev/null; then
          echo "  ✅ Valid JSON"
        else
          local json_err
          json_err=$(jq empty "$output_file" 2>&1 || true)
          echo "  ❌ Invalid JSON: $json_err"
          issues+=("Task $task_id ($task_file): Invalid JSON — $json_err")
        fi
        ;;

      *.ts|*.tsx)
        if [[ "$task_file" =~ \.(test|spec)\.(ts|tsx)$ ]]; then
          local has_vitest has_jest
          has_vitest=$(grep -c "from 'vitest'\|from \"vitest\"\|vi\.fn\|vi\.mock\|vi\.spyOn" "$output_file" 2>/dev/null || echo 0)
          has_jest=$(grep -c "jest\.fn\|jest\.mock\|jest\.spyOn\|jest\.SpyInstance\|from '@jest" "$output_file" 2>/dev/null || echo 0)
          if [[ ${has_vitest:-0} -gt 0 && ${has_jest:-0} -gt 0 ]]; then
            echo "  ❌ Mixes Vitest and Jest syntax"
            issues+=("Task $task_id ($task_file): Mixes Vitest and Jest syntax — will cause runtime errors")
          elif [[ ${has_vitest:-0} -gt 0 ]]; then
            echo "  ✅ Vitest syntax consistent"
          elif [[ ${has_jest:-0} -gt 0 ]]; then
            echo "  ✅ Jest syntax consistent"
          fi
        fi

        echo "  ✅ TypeScript checks done"
        ;;

      *.js|*.jsx)
        if command -v node &> /dev/null; then
          if node --check "$output_file" 2>/dev/null; then
            echo "  ✅ JavaScript syntax valid"
          else
            local js_err
            js_err=$(node --check "$output_file" 2>&1 || true)
            echo "  ❌ JavaScript syntax error"
            issues+=("Task $task_id ($task_file): JavaScript syntax error — $js_err")
          fi
        fi
        ;;
    esac

    if [[ "$task_file" =~ \.(ts|tsx|js|jsx)$ ]]; then

      if [[ -f "package.json" ]]; then
        while IFS= read -r pkg_import; do
          if [[ -n "$pkg_import" ]]; then
            local pkg_name="$pkg_import"
            [[ "$pkg_import" == @* ]] && \
              pkg_name=$(echo "$pkg_import" | cut -d'/' -f1,2) || \
              pkg_name=$(echo "$pkg_import" | cut -d'/' -f1)

            local node_builtins="fs path os crypto http https url util stream events child_process process buffer"
            echo "$node_builtins" | grep -qw "$pkg_name" && continue

            if ! jq -e \
              ".dependencies[\"$pkg_name\"] // .devDependencies[\"$pkg_name\"] // .peerDependencies[\"$pkg_name\"]" \
              package.json > /dev/null 2>&1; then
              echo "  ⚠️  '$pkg_name' not in package.json"
              warnings+=("Task $task_id: Imports '$pkg_name' not found in package.json")
            fi
          fi
        done < <(grep -E "^import .* from ['\"]([^./][^'\"]*)['\"]" "$output_file" 2>/dev/null \
          | sed "s/.*from ['\"]\\([^'\"]*\\)['\"].*/\\1/" || true)
      fi

      while IFS= read -r rel_import; do
        if [[ -n "$rel_import" ]]; then
          local file_dir
          file_dir=$(dirname "$task_file")
          local import_path
          import_path=$(echo "$file_dir/$rel_import" | sed 's|/\./|/|g; s|/[^/]*/\.\./|/|g')

          local base="${import_path%.ts}"
          base="${base%.tsx}" base="${base%.js}" base="${base%.jsx}"

          if [[ -f "$base" || -f "$base.ts" || -f "$base.tsx" || \
                -f "$base.js" || -f "$base.jsx" || \
                -f "$base/index.ts" || -f "$base/index.tsx" || -f "$base/index.js" ]]; then
            continue
          fi

          local resolved_by_session=false
          for other_id in "${task_ids[@]}"; do
            local other_file
            other_file=$(jq -r ".tasks[]? | select(.id == \"$other_id\") | .file" "$tasks_file")
            other_file=$(_apply_clean_path "$other_file")
            local other_base="${other_file%.ts}"
            other_base="${other_base%.tsx}" other_base="${other_base%.js}" other_base="${other_base%.jsx}"
            if [[ "$other_base" == "$base" ]]; then
              resolved_by_session=true
              break
            fi
          done

          if [[ "$resolved_by_session" == false ]]; then
            echo "  ⚠️  Relative import not found: $rel_import"
            warnings+=("Task $task_id: Relative import '$rel_import' does not resolve to a file")
          fi
        fi
      done < <(grep -E "^import .* from ['\"](\.[^'\"]*)['\"]" "$output_file" 2>/dev/null \
        | sed "s/.*from ['\"]\\([^'\"]*\\)['\"].*/\\1/" || true)

      local deps
      deps=$(echo "$task_json" | jq -r '.dependencies[]?' 2>/dev/null || true)
      for dep in $deps; do
        local dep_output="$session_dir/outputs/task_${dep}.txt"
        [[ ! -f "$dep_output" ]] && continue

        local dep_file
        dep_file=$(jq -r ".tasks[]? | select(.id == \"$dep\") | .file" "$tasks_file")
        dep_file=$(_apply_clean_path "$dep_file")
        local dep_base
        dep_base=$(basename "${dep_file%.*}")

        while IFS= read -r imported_symbol; do
          [[ -z "$imported_symbol" ]] && continue
          if ! grep -qE "export (const|function|class|type|interface|enum) $imported_symbol" "$dep_output" 2>/dev/null; then
            echo "  ⚠️  Imports '$imported_symbol' from task $dep but it's not exported there"
            warnings+=("Task $task_id: Imports '$imported_symbol' from task $dep output but export not found")
          fi
        done < <(grep -E "import \{[^}]+\} from" "$output_file" 2>/dev/null \
          | grep "$dep_base" 2>/dev/null \
          | sed "s/.*{\([^}]*\)}.*/\1/" \
          | tr ',' '\n' \
          | tr -d ' ' || true)
      done
    fi

    echo ""
  done

  if [[ ${#issues[@]} -gt 0 ]]; then
    printf '%s\n' "${issues[@]}" > "$session_dir/validation_issues.txt"
  else
    rm -f "$session_dir/validation_issues.txt"
  fi

  if [[ ${#warnings[@]} -gt 0 ]]; then
    printf '%s\n' "${warnings[@]}" > "$session_dir/validation_warnings.txt"
  else
    rm -f "$session_dir/validation_warnings.txt"
  fi

  echo "─────────────────────────────────────"

  if [[ ${#issues[@]} -eq 0 && ${#warnings[@]} -eq 0 ]]; then
    echo "✅ All validations passed"
    return 0
  elif [[ ${#issues[@]} -eq 0 ]]; then
    echo "⚠️  ${#warnings[@]} warning(s) — review before applying"
    echo ""
    printf '  • %s\n' "${warnings[@]}"
    echo ""
    echo "Run 'apply' to proceed or fix warnings first"
    return 0
  else
    echo "❌ ${#issues[@]} critical issue(s) found"
    [[ ${#warnings[@]} -gt 0 ]] && echo "⚠️  Plus ${#warnings[@]} warning(s)"
    echo ""
    echo "Issues:"
    printf '  • %s\n' "${issues[@]}"
    if [[ ${#warnings[@]} -gt 0 ]]; then
      echo ""
      echo "Warnings:"
      printf '  • %s\n' "${warnings[@]}"
    fi
    echo ""
    echo "Run 'refine' to fix or manually edit: $session_dir/outputs/"
    return 1
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# AI reviewer — runs after static validation, or on-demand against git diff
# ─────────────────────────────────────────────────────────────────────────────

function review() {
  [[ -f "$AGENTIC_HOME/.agentic.conf" ]] && source "$AGENTIC_HOME/.agentic.conf"

  local force_diff=false
  local base_ref="HEAD"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --diff)        force_diff=true ; shift ;;
      --diff=*)      force_diff=true ; base_ref="${1#--diff=}" ; shift ;;
      *)             shift ;;
    esac
  done

  local reviewer_prompt
  reviewer_prompt="$(cat "$AGENTIC_HOME/agents/reviewer.txt")"
  if [[ -f "CLAUDE.md" ]]; then
    reviewer_prompt="$reviewer_prompt

PROJECT DOCUMENTATION (CLAUDE.md):
$(cat CLAUDE.md)"
  fi

  local session_dir
  session_dir=$(_apply_resolve_session 2>/dev/null || echo "")

  local use_diff=false
  if [[ "$force_diff" == true ]]; then
    use_diff=true
  elif [[ -z "$session_dir" ]] || \
       [[ ! -d "$session_dir/outputs" ]] || \
       [[ -z "$(ls "$session_dir/outputs/"task_*.txt 2>/dev/null)" ]]; then
    use_diff=true
  fi

  if [[ "$use_diff" == true ]]; then
    echo "🔎 Running AI review (diff mode: $base_ref)..."
    echo ""

    if ! git rev-parse --is-inside-work-tree &>/dev/null; then
      echo "❌ Not a git repository — diff mode requires git"
      return 1
    fi

    local git_diff
    git_diff=$(git diff "$base_ref" 2>/dev/null || true)
    if [[ -z "$git_diff" ]]; then
      git_diff=$(git diff --staged 2>/dev/null || true)
    fi

    if [[ -z "$git_diff" ]]; then
      echo "⚠️  No changes detected (git diff $base_ref is empty)"
      return 0
    fi

    local changed_files
    changed_files=$(git diff --name-only "$base_ref" 2>/dev/null || true)
    local line_count
    line_count=$(echo "$git_diff" | wc -l | tr -d ' ')
    echo "📄 Changed files:"
    echo "$changed_files" | sed 's/^/   /'
    echo "   ($line_count lines of diff)"
    echo ""

    local review_input
    review_input="CHANGES BEING REVIEWED (git diff $base_ref):

$git_diff"

    local review_output
    review_output="$(pwd)/.claude/review-$(date +%Y%m%d-%H%M%S).txt"
    mkdir -p "$(pwd)/.claude"

    claude_api \
      --model "$AGENTIC_MODEL" \
      --system "$reviewer_prompt" \
      --cache-system \
      --temperature 0.3 \
      --user "$review_input" \
      --output "$review_output" \
      --usage "${review_output%.txt}_usage.json"

    if [[ $? -ne 0 ]]; then
      echo "❌ Review API call failed"
      return 1
    fi

    echo ""
    cat "$review_output"
    echo ""
    echo "📄 Review saved: $review_output"
    return 0
  fi

  echo "🔎 Running AI review (session mode)..."
  echo ""

  local tasks_file="$session_dir/tasks.json"
  local request_file="$session_dir/request.txt"

  if [[ ! -f "$tasks_file" || ! -f "$request_file" ]]; then
    echo "⚠️  Missing tasks or request — skipping review"
    return 0
  fi

  local task_ids
  task_ids=($(_apply_get_task_ids "$tasks_file"))

  local review_input
  review_input="ORIGINAL REQUEST:
$(cat "$request_file")

TASK PLAN:
$(jq -r '.tasks[] | "[\(.id)] \(.action) \(.file) (\(.modification_type)) — \(.description)"' "$tasks_file" 2>/dev/null)

GENERATED CODE:"

  for task_id in "${task_ids[@]}"; do
    local output_file="$session_dir/outputs/task_${task_id}.txt"
    [[ ! -f "$output_file" ]] && continue
    local task_file modification_type
    task_file=$(jq -r ".tasks[]? | select(.id == \"$task_id\") | .file" "$tasks_file")
    modification_type=$(jq -r ".tasks[]? | select(.id == \"$task_id\") | .modification_type" "$tasks_file")
    review_input+="

--- [$task_id] $task_file ($modification_type) ---
$(cat "$output_file")"
  done

  local review_output="$session_dir/review.txt"

  claude_api \
    --model "$AGENTIC_MODEL" \
    --system "$reviewer_prompt" \
    --cache-system \
    --temperature 0.3 \
    --user "$review_input" \
    --output "$review_output" \
    --usage "$session_dir/review_usage.json"

  if [[ $? -ne 0 ]]; then
    echo "⚠️  Review API call failed — skipping"
    return 0
  fi

  cat "$review_output"
  echo ""

  # Verdict parsing — every grep needs `|| true` for the same reason as
  # everywhere else: no-match returns 1, pipefail propagates, set -e kills.
  local verdict=""
  verdict=$(grep "VERDICT:" "$review_output" 2>/dev/null | grep -oE 'APPROVED|REJECTED' | tail -1 || true)

  case "$verdict" in
    REJECTED)
      echo "❌ Review: REJECTED"
      local critical=""
      critical=$(awk '/### Critical Issues/,/### (Warnings|Positive|Final|##)/' "$review_output" 2>/dev/null \
        | grep -E '^\s*[0-9]+\.' | sed 's/^[[:space:]]*//' | head -10 || true)
      local issues_file="$session_dir/validation_issues.txt"
      if [[ -n "$critical" ]]; then
        echo "$critical" >> "$issues_file"
      else
        echo "Review rejected implementation — see $session_dir/review.txt" >> "$issues_file"
      fi
      return 1
      ;;
    APPROVED)
      echo "✅ Review: APPROVED"
      local warnings=""
      warnings=$(awk '/### Warnings/,/### (Positive|Final|##)/' "$review_output" 2>/dev/null \
        | grep -E '^\s*[0-9]+\.' | sed 's/^[[:space:]]*//' | head -10 || true)
      if [[ -n "$warnings" ]]; then
        echo "$warnings" >> "$session_dir/validation_warnings.txt"
      fi
      return 0
      ;;
    *)
      echo "⚠️  Could not parse review verdict — treating as passed"
      return 0
      ;;
  esac
}
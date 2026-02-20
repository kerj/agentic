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

    echo "Task $task_id ($modification_type): $task_desc"

    # ── delete_code: expect a stitched file or empty for full delete ─────────
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

    # ── Output must exist and have content ───────────────────────────────────
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

    # ── Stray markdown fences ────────────────────────────────────────────────
    if grep -q '```' "$output_file"; then
      echo "  ⚠️  Contains markdown fences"
      warnings+=("Task $task_id: Output contains markdown fences")
    fi

    # ── Placeholder / incomplete code ────────────────────────────────────────
    if grep -qiE \
      '^\s*\.\.\.$|\.\.\..*rest.*code|\.\.\..*omitted|\.\.\..*more|// TODO|// your code here|// implement|/\* implement|// placeholder|// add implementation' \
      "$output_file"; then
      echo "  ❌ Contains placeholder text — output is incomplete"
      issues+=("Task $task_id ($task_file): Output contains placeholder text or TODO markers")
    fi

    # ── File path must have extension ────────────────────────────────────────
    if [[ "$task_action" != "DELETE" && ! "$task_file" =~ \. ]]; then
      echo "  ❌ File path has no extension: $task_file"
      issues+=("Task $task_id: File path '$task_file' has no extension")
    fi

    # ── modification_type shape checks ───────────────────────────────────────
    case "$modification_type" in
      add_import)
        if [[ $output_lines -gt 3 ]]; then
          echo "  ⚠️  add_import output is $output_lines lines — expected 1"
          warnings+=("Task $task_id: add_import should be ~1 line, got $output_lines")
        else
          echo "  ✅ Import shape correct ($output_lines line(s))"
        fi

        # Duplicate import — check if this import already exists in source file
        if [[ -f "$task_file" ]]; then
          local import_symbol
          import_symbol=$(grep -oE '\{[^}]+\}' "$output_file" | head -1 | tr -d '{ }')
          if [[ -n "$import_symbol" ]] && grep -q "$import_symbol" "$task_file"; then
            echo "  ⚠️  '$import_symbol' may already be imported in $task_file"
            warnings+=("Task $task_id: '$import_symbol' may already be imported — could cause duplicate")
          fi
        fi
        ;;

      add_route)
        if [[ $output_lines -gt 3 ]]; then
          echo "  ⚠️  add_route output is $output_lines lines — expected 1"
          warnings+=("Task $task_id: add_route should be ~1 line, got $output_lines")
        else
          echo "  ✅ Route shape correct ($output_lines line(s))"
        fi
        ;;

      add_export)
        if [[ $output_lines -gt 5 ]]; then
          echo "  ⚠️  add_export seems large ($output_lines lines)"
          warnings+=("Task $task_id: add_export seems large at $output_lines lines")
        fi
        ;;

      modify_function|add_to_function|add_hook|wrap_component)
        # These should NOT contain import statements — that means full file was output
        if grep -qE "^import " "$output_file"; then
          echo "  ❌ $modification_type output contains import statements — model output full file instead of just the function"
          issues+=("Task $task_id ($task_file): $modification_type output contains imports — stitching will corrupt the file")
        fi

        # Target function name should appear in output
        if [[ -n "$target" && "$target" != "null" ]]; then
          if ! grep -q "$target" "$output_file"; then
            echo "  ⚠️  Target '$target' not found in output — model may have rewritten wrong function"
            warnings+=("Task $task_id: Target '$target' not found in output")
          fi
        fi
        ;;
    esac

    # ── Size regression for MODIFY ───────────────────────────────────────────
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

    # ── Language-specific validation ─────────────────────────────────────────
    case "$task_file" in
      *.json)
        if jq empty "$output_file" 2>/dev/null; then
          echo "  ✅ Valid JSON"
        else
          local json_err
          json_err=$(jq empty "$output_file" 2>&1)
          echo "  ❌ Invalid JSON: $json_err"
          issues+=("Task $task_id ($task_file): Invalid JSON — $json_err")
        fi
        ;;

      *.ts|*.tsx)
        # Brace balance — only meaningful for complete function/file outputs
        if [[ "$modification_type" == "full_file" || \
              "$modification_type" == "add_function" || \
              "$modification_type" == "modify_function" ]]; then
          local opens closes
          opens=$(grep -o '{' "$output_file" | wc -l | tr -d ' ')
          closes=$(grep -o '}' "$output_file" | wc -l | tr -d ' ')
          if [[ $opens -ne $closes ]]; then
            echo "  ⚠️  Unbalanced braces (${opens}{ vs ${closes}})"
            warnings+=("Task $task_id ($task_file): Unbalanced braces — may be incomplete")
          else
            echo "  ✅ Braces balanced"
          fi
        fi

        # Test framework mixing
        if [[ "$task_file" =~ \.(test|spec)\.(ts|tsx)$ ]]; then
          local has_vitest has_jest
          has_vitest=$(grep -c "from 'vitest'\|from \"vitest\"\|vi\.fn\|vi\.mock\|vi\.spyOn" "$output_file" 2>/dev/null || true)
          has_jest=$(grep -c "jest\.fn\|jest\.mock\|jest\.spyOn\|jest\.SpyInstance\|from '@jest" "$output_file" 2>/dev/null || true)
          if [[ $has_vitest -gt 0 && $has_jest -gt 0 ]]; then
            echo "  ❌ Mixes Vitest and Jest syntax"
            issues+=("Task $task_id ($task_file): Mixes Vitest and Jest syntax — will cause runtime errors")
          elif [[ $has_vitest -gt 0 ]]; then
            echo "  ✅ Vitest syntax consistent"
          elif [[ $has_jest -gt 0 ]]; then
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
            js_err=$(node --check "$output_file" 2>&1)
            echo "  ❌ JavaScript syntax error"
            issues+=("Task $task_id ($task_file): JavaScript syntax error — $js_err")
          fi
        fi
        ;;
    esac

    # ── Import validation for TS/JS files ────────────────────────────────────
    if [[ "$task_file" =~ \.(ts|tsx|js|jsx)$ ]]; then

      # Package imports — must be in package.json
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
        done < <(grep -E "^import .* from ['\"]([^./][^'\"]*)['\"]" "$output_file" \
          | sed "s/.*from ['\"]\\([^'\"]*\\)['\"].*/\\1/")
      fi

      # Relative imports — check against disk AND session outputs
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

          # Check if another task in this session is creating that file
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
      done < <(grep -E "^import .* from ['\"](\.[^'\"]*)['\"]" "$output_file" \
        | sed "s/.*from ['\"]\\([^'\"]*\\)['\"].*/\\1/")

      # Cross-task symbol consistency — check exports in dependencies match imports here
      local deps
      deps=$(echo "$task_json" | jq -r '.dependencies[]?' 2>/dev/null)
      for dep in $deps; do
        local dep_output="$session_dir/outputs/task_${dep}.txt"
        [[ ! -f "$dep_output" ]] && continue

        # Find what this task imports from the dep's file
        local dep_file
        dep_file=$(jq -r ".tasks[]? | select(.id == \"$dep\") | .file" "$tasks_file")
        dep_file=$(_apply_clean_path "$dep_file")
        local dep_base
        dep_base=$(basename "${dep_file%.*}")

        while IFS= read -r imported_symbol; do
          [[ -z "$imported_symbol" ]] && continue
          # Check the symbol is exported in the dep's output
          if ! grep -qE "export (const|function|class|type|interface|enum) $imported_symbol" "$dep_output"; then
            echo "  ⚠️  Imports '$imported_symbol' from task $dep but it's not exported there"
            warnings+=("Task $task_id: Imports '$imported_symbol' from task $dep output but export not found")
          fi
        done < <(grep -E "import \{[^}]+\} from" "$output_file" \
          | grep "$dep_base" \
          | sed "s/.*{\([^}]*\)}.*/\1/" \
          | tr ',' '\n' \
          | tr -d ' ')
      done
    fi

    echo ""
  done

  # ── Save issues for refine ────────────────────────────────────────────────
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

  # ── Summary ───────────────────────────────────────────────────────────────
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
#!/bin/bash
# Apply functions

# ─────────────────────────────────────────────────────────────────────────────
# apply
# ─────────────────────────────────────────────────────────────────────────────

function apply() {
  local dry_run=false
  [[ "${1:-}" == "--dry-run" ]] && dry_run=true

  local session_dir
  session_dir=$(_apply_resolve_session)
  if [[ -z "$session_dir" ]]; then
    echo "❌ No active session."
    return 1
  fi

  local tasks_file="$session_dir/tasks.json"
  [[ ! -f "$tasks_file" ]] && echo "❌ No tasks found." && return 1

  if [[ "$dry_run" == true ]]; then
    echo "🔍 DRY RUN — no files will be modified"
  else
    echo "🔧 Applying changes from session: $AGENTIC_SESSION"
  fi
  echo ""

  local task_ids
  task_ids=($(_apply_get_task_ids "$tasks_file"))

  # ── Pre-apply validation ──────────────────────────────────────────────────
  echo "🔍 Pre-apply validation..."
  local precheck_failed=false
  local precheck_issues=()

  for task_id in "${task_ids[@]}"; do
    local task_json
    task_json=$(jq ".tasks[]? | select(.id == \"$task_id\")" "$tasks_file")
    local task_file task_action modification_type
    task_file=$(echo "$task_json" | jq -r '.file')
    task_action=$(echo "$task_json" | jq -r '.action')
    modification_type=$(echo "$task_json" | jq -r '.modification_type // "full_file"')
    local output_file="$session_dir/outputs/task_${task_id}.txt"

    # File path must have an extension
    if [[ "$task_action" != "DELETE" && ! "$task_file" =~ \. ]]; then
      echo "  ⚠️  Task $task_id: '$task_file' has no extension"
      precheck_issues+=("Task $task_id: File path '$task_file' has no extension")
      precheck_failed=true
    fi

    # Output file must exist for non-DELETE tasks
    if [[ "$task_action" != "DELETE" && "$modification_type" != "delete_code" && ! -f "$output_file" ]]; then
      echo "  ⚠️  Task $task_id: No output file at $output_file"
      precheck_issues+=("Task $task_id: No output file generated")
      precheck_failed=true
    fi
  done

  if [[ "$precheck_failed" == true ]]; then
    echo ""
    echo "❌ Pre-apply validation failed"
    printf '%s\n' "${precheck_issues[@]}" > "$session_dir/validation_issues.txt"
    echo ""
    printf '  • %s\n' "${precheck_issues[@]}"
    echo ""
    echo "Run 'refine' to regenerate plan with fixes"
    return 1
  fi

  echo "✅ Pre-apply validation passed"
  echo ""

  # ── Git safety ────────────────────────────────────────────────────────────
  local branch_name=""
  local original_commit=""

  if [[ "$dry_run" == false ]] && git rev-parse --git-dir > /dev/null 2>&1; then
    if ! git diff-index --quiet HEAD --; then
      echo "⚠️  Uncommitted changes detected:"
      git status --short
      echo ""
      read -p "Continue anyway? (y/n) " proceed_dirty
      [[ ! "$proceed_dirty" =~ ^[Yy]$ ]] && echo "Commit first, then run 'apply'" && return 1
    fi

    original_commit=$(git rev-parse HEAD)
    branch_name="agentic/${AGENTIC_SESSION}"

    if git show-ref --verify --quiet "refs/heads/$branch_name"; then
      echo "⚠️  Branch $branch_name already exists"
      read -p "Use it anyway? (y/n) " use_existing
      [[ ! "$use_existing" =~ ^[Yy]$ ]] && echo "Cancelled" && return 1
      git checkout "$branch_name"
    else
      git checkout -b "$branch_name"
    fi

    echo "📌 Branch: $branch_name"
    echo "📌 Rollback: git reset --hard $original_commit"
    echo ""
  elif [[ "$dry_run" == false ]]; then
    echo "⚠️  Not a git repository"
    read -p "Continue anyway? (y/n) " continue_no_git
    [[ ! "$continue_no_git" =~ ^[Yy]$ ]] && return 1
    echo ""
  fi

  # ── Apply tasks ───────────────────────────────────────────────────────────
  local total=${#task_ids[@]}
  local applied=0
  local skipped=0
  local failed=0
  local counter=0
  local files_staged=()   # track files touched so we stage only those at commit

  echo "📋 $([ "$dry_run" = true ] && echo "Previewing" || echo "Applying") $total tasks..."
  echo ""

  for task_id in "${task_ids[@]}"; do
    ((counter++))

    local task_json
    task_json=$(jq ".tasks[]? | select(.id == \"$task_id\")" "$tasks_file")
    local task_file task_action task_desc modification_type
    task_file=$(echo "$task_json" | jq -r '.file')
    task_action=$(echo "$task_json" | jq -r '.action')
    task_desc=$(echo "$task_json" | jq -r '.description // "no description"')
    modification_type=$(echo "$task_json" | jq -r '.modification_type // "full_file"')

    task_file=$(_apply_clean_path "$task_file")

    echo "[$counter/$total] $task_desc"
    echo "  File: $task_file ($task_action / $modification_type)"

    local output_file="$session_dir/outputs/task_${task_id}.txt"

    if [[ "$dry_run" == true ]]; then
      case "$task_action" in
        CREATE)
          local lines
          lines=$(wc -l < "$output_file" | tr -d ' ')
          echo "  Would create ($lines lines)"
          head -5 "$output_file" | sed 's/^/    /'
          [[ $lines -gt 5 ]] && echo "    ... ($((lines - 5)) more lines)"
          ;;
        MODIFY)
          local out_lines orig_lines
          out_lines=$(wc -l < "$output_file" | tr -d ' ')
          orig_lines=$(wc -l < "$task_file" 2>/dev/null | tr -d ' ')
          echo "  Would modify: $orig_lines lines → $out_lines lines"
          ;;
        DELETE)
          echo "  Would delete"
          ;;
      esac
      ((applied++))
    else
      case "$task_action" in
        CREATE)
          if [[ -f "$task_file" ]]; then
            echo "  ⚠️  File exists — backing up"
            cp "$task_file" "$task_file.backup"
          fi
          mkdir -p "$(dirname "$task_file")"
          cp "$output_file" "$task_file"
          echo "  ✅ Created"
          files_staged+=("$task_file")
          ((applied++))
          ;;

        MODIFY)
          if [[ ! -f "$task_file" ]]; then
            echo "  ⚠️  File not found — creating instead"
            mkdir -p "$(dirname "$task_file")"
            cp "$output_file" "$task_file"
          else
            cp "$task_file" "$task_file.backup"
            cp "$output_file" "$task_file"
            echo "  ✅ Modified"
          fi
          files_staged+=("$task_file")
          ((applied++))
          ;;

        DELETE)
          if [[ -f "$output_file" && -s "$output_file" ]]; then
            # implement.sh produced a stitched file with target removed — apply it
            cp "$task_file" "$task_file.backup"
            cp "$output_file" "$task_file"
            echo "  ✅ Target removed from file (backup saved)"
            files_staged+=("$task_file")
          elif [[ -f "$task_file" ]]; then
            # Full file deletion
            mv "$task_file" "$task_file.backup"
            echo "  ✅ File deleted (backup saved)"
            files_staged+=("$task_file.backup")
          else
            echo "  ⊘ File not found — already deleted?"
            ((skipped++))
            echo ""
            continue
          fi
          ((applied++))
          ;;

        *)
          echo "  ❌ Unknown action: $task_action"
          ((failed++))
          ;;
      esac
    fi

    echo ""
  done

  # ── Summary ───────────────────────────────────────────────────────────────
  echo "─────────────────────────────────────"
  if [[ "$dry_run" == true ]]; then
    echo "📊 Would apply: $applied"
    [[ $skipped -gt 0 ]] && echo "⊘ Would skip: $skipped"
    echo ""
    echo "Run 'apply' without --dry-run to make these changes"
    return 0
  fi

  echo "✅ Applied: $applied"
  [[ $skipped -gt 0 ]] && echo "⊘ Skipped: $skipped"
  [[ $failed -gt 0 ]] && echo "❌ Failed: $failed"
  echo ""

  # ── Git commit ────────────────────────────────────────────────────────────
  if git rev-parse --git-dir > /dev/null 2>&1; then
    echo "📊 Changes:"
    git diff --stat
    echo ""
    git status --short
    echo ""

    read -p "Commit these changes? (y/n) " do_commit
    if [[ "$do_commit" =~ ^[Yy]$ ]]; then
      for _f in "${files_staged[@]}"; do
        git add "$_f" 2>/dev/null || true
      done
      git commit -m "agentic: $AGENTIC_SESSION"
      echo "✅ Committed"
      echo ""
      echo "  Merge:    git checkout main && git merge $branch_name"
      echo "  Rollback: git reset --hard $original_commit"
    else
      echo "Not committed."
      echo "  Commit:   git add -A && git commit -m 'your message'"
      echo "  Rollback: git reset --hard $original_commit"
      echo "  Discard:  git checkout main && git branch -D $branch_name"
    fi
  fi

  # ── Verify ────────────────────────────────────────────────────────────────
  if [[ $failed -eq 0 && $applied -gt 0 ]]; then
    echo ""
    echo "🔍 Verifying applied changes..."
    verify-apply
    if [[ $? -ne 0 ]]; then
      echo ""
      echo "⚠️  Some changes may not have applied correctly"
      echo "   Run 'verify-apply' for details"
    fi
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# verify-apply
# ─────────────────────────────────────────────────────────────────────────────

function verify-apply() {
  local session_dir
  session_dir=$(_apply_resolve_session)
  if [[ -z "$session_dir" ]]; then
    echo "❌ No active session."
    return 1
  fi

  local tasks_file="$session_dir/tasks.json"
  [[ ! -f "$tasks_file" ]] && echo "❌ No tasks found." && return 1

  echo "🔍 Verifying applied changes..."
  echo ""

  local task_ids
  task_ids=($(_apply_get_task_ids "$tasks_file"))
  local mismatches=0

  for task_id in "${task_ids[@]}"; do
    local task_json
    task_json=$(jq ".tasks[]? | select(.id == \"$task_id\")" "$tasks_file")
    local task_file task_action task_desc modification_type
    task_file=$(echo "$task_json" | jq -r '.file')
    task_action=$(echo "$task_json" | jq -r '.action')
    task_desc=$(echo "$task_json" | jq -r '.description // "no description"')
    modification_type=$(echo "$task_json" | jq -r '.modification_type // "full_file"')

    task_file=$(_apply_clean_path "$task_file")

    echo "Task $task_id ($modification_type): $task_desc"
    echo "  File: $task_file"

    local output_file="$session_dir/outputs/task_${task_id}.txt"

    case "$task_action" in
      CREATE)
        if [[ -f "$task_file" ]]; then
          if [[ -f "$output_file" ]] && diff -q "$task_file" "$output_file" > /dev/null 2>&1; then
            echo "  ✅ Created and content matches"
          elif [[ -f "$output_file" ]]; then
            echo "  ⚠️  File exists but content differs from generated output"
            ((mismatches++))
          else
            echo "  ✅ File exists"
          fi
        else
          echo "  ❌ File was not created"
          local basename
          basename=$(basename "$task_file")
          local found
          found=$(find . -name "$basename" -not -path "*/node_modules/*" \
            -not -path "*/.git/*" 2>/dev/null | head -3)
          [[ -n "$found" ]] && echo "     Similar files found: $found"
          ((mismatches++))
        fi
        ;;

      MODIFY)
        if [[ ! -f "$task_file" ]]; then
          echo "  ❌ File does not exist: $task_file"
          ((mismatches++))
        elif [[ -f "$task_file.backup" ]] && diff -q "$task_file" "$task_file.backup" > /dev/null 2>&1; then
          echo "  ⚠️  File was not modified (matches backup)"
          ((mismatches++))
        else
          echo "  ✅ Modified"
        fi
        ;;

      DELETE)
        if [[ "$modification_type" == "delete_code" ]]; then
          # Partial delete — file should still exist but be different from backup
          if [[ ! -f "$task_file" ]]; then
            echo "  ❌ File does not exist"
            ((mismatches++))
          elif [[ -f "$task_file.backup" ]] && diff -q "$task_file" "$task_file.backup" > /dev/null 2>&1; then
            echo "  ⚠️  File unchanged — target may not have been removed"
            ((mismatches++))
          else
            echo "  ✅ Target removed from file"
          fi
        else
          # Full file delete
          if [[ ! -f "$task_file" ]]; then
            echo "  ✅ File deleted"
          else
            echo "  ❌ File still exists"
            ((mismatches++))
          fi
        fi
        ;;
    esac

    echo ""
  done

  echo "─────────────────────────────────────"
  if [[ $mismatches -eq 0 ]]; then
    echo "✅ All changes verified"
    return 0
  else
    echo "❌ $mismatches mismatch(es) found"
    echo "   Run 'refine' or manually fix then re-apply"
    return 1
  fi
}
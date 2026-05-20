#!/bin/bash
# Apply functions

# ─────────────────────────────────────────────────────────────────────────────
# apply
# Workers write to an isolated git worktree and commit there. The user's
# working tree is never touched. Review the branch, then run 'agentic accept'
# or 'agentic reject' on your schedule.
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
  export AGENTIC_SESSION="${AGENTIC_SESSION:-$(basename "${session_dir%/}")}"

  local tasks_file="$session_dir/tasks.json"
  [[ ! -f "$tasks_file" ]] && echo "❌ No tasks found." && return 1

  # In-place mode (set by worker-once): apply directly to the current directory,
  # which is already the agentic/<id> branch worktree. Skips worktree creation.
  local in_place=false
  [[ "${AGENTIC_APPLY_IN_PLACE:-}" == "1" ]] && in_place=true

  local branch_name
  local worktree_path
  if [[ "$in_place" == true ]]; then
    branch_name=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "agentic/${AGENTIC_SESSION}")
    worktree_path="."
  else
    branch_name="agentic/${AGENTIC_SESSION}"
    worktree_path=".claude/worktrees/${AGENTIC_SESSION}"
  fi

  if [[ "$dry_run" == true ]]; then
    echo "🔍 DRY RUN — no files will be modified"
  else
    echo "🔧 Applying session: $AGENTIC_SESSION → $branch_name"
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

    if [[ "$task_action" != "DELETE" && ! "$task_file" =~ \. ]]; then
      echo "  ⚠️  Task $task_id: '$task_file' has no extension"
      precheck_issues+=("Task $task_id: File path '$task_file' has no extension")
      precheck_failed=true
    fi

    if [[ "$task_action" != "DELETE" && "$modification_type" != "delete_code" && \
          ! -f "$output_file" ]]; then
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

  # ── Dry run preview ───────────────────────────────────────────────────────
  if [[ "$dry_run" == true ]]; then
    local counter=0
    local total=${#task_ids[@]}
    echo "📋 Previewing $total tasks..."
    echo ""
    for task_id in "${task_ids[@]}"; do
      ((counter++))
      local task_json
      task_json=$(jq ".tasks[]? | select(.id == \"$task_id\")" "$tasks_file")
      local task_file task_action task_desc
      task_file=$(echo "$task_json" | jq -r '.file')
      task_action=$(echo "$task_json" | jq -r '.action')
      task_desc=$(echo "$task_json" | jq -r '.description // "no description"')
      task_file=$(_apply_clean_path "$task_file")
      local output_file="$session_dir/outputs/task_${task_id}.txt"

      echo "[$counter/$total] $task_desc"
      echo "  File: $task_file ($task_action)"
      case "$task_action" in
        CREATE)
          local lines; lines=$(wc -l < "$output_file" | tr -d ' ')
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
        DELETE) echo "  Would delete" ;;
      esac
      echo ""
    done
    echo "─────────────────────────────────────"
    echo "📊 Would apply: $counter tasks to worktree $branch_name"
    echo ""
    echo "Run 'apply' without --dry-run to commit to the worktree"
    return 0
  fi

  # ── Require git ──────────────────────────────────────────────────────────
  if ! git rev-parse --git-dir > /dev/null 2>&1; then
    echo "❌ Not a git repository — worktree apply requires git"
    return 1
  fi

  if [[ "$in_place" == true ]]; then
    # Already in the agentic branch worktree — no creation needed.
    echo "📌 Applying in-place to branch: $branch_name"
    echo ""
  else
    # Warn if the user has uncommitted changes — they won't be in the worktree
    # base since the worktree branches from HEAD.
    if ! git diff-index --quiet HEAD --; then
      echo "⚠️  You have uncommitted changes in your working tree."
      echo "   The worktree will branch from HEAD — your unstaged changes"
      echo "   will not be visible to the generated code."
      echo ""
      if [[ "${AGENTIC_NON_INTERACTIVE:-}" == "1" ]]; then
        echo "   Continuing in non-interactive mode."
        echo ""
      else
        read -p "Continue anyway? (y/n) " proceed_dirty
        [[ ! "$proceed_dirty" =~ ^[Yy]$ ]] && echo "Commit or stash first, then run 'apply'" && return 1
        echo ""
      fi
    fi

    # ── Create worktree ─────────────────────────────────────────────────────
    mkdir -p ".claude/worktrees"

    if [[ -d "$worktree_path" ]]; then
      echo "⚠️  Worktree already exists at $worktree_path — reusing"
    elif git show-ref --verify --quiet "refs/heads/$branch_name"; then
      echo "⚠️  Branch $branch_name already exists — creating worktree for it"
      git worktree add "$worktree_path" "$branch_name"
    else
      git worktree add -b "$branch_name" "$worktree_path"
    fi

    echo "📌 Worktree: $worktree_path"
    echo "📌 Branch:   $branch_name"
    echo ""
  fi

  # ── Apply tasks into worktree ─────────────────────────────────────────────
  local total=${#task_ids[@]}
  local applied=0
  local skipped=0
  local failed=0
  local counter=0
  local files_staged=()

  echo "📋 Applying $total tasks..."
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
    local dest="$worktree_path/$task_file"
    local output_file="$session_dir/outputs/task_${task_id}.txt"

    echo "[$counter/$total] $task_desc"
    echo "  File: $task_file ($task_action / $modification_type)"

    case "$task_action" in
      CREATE)
        mkdir -p "$(dirname "$dest")"
        cp "$output_file" "$dest"
        echo "  ✅ Created"
        files_staged+=("$task_file")
        ((applied++))
        ;;

      MODIFY)
        if [[ ! -f "$dest" ]]; then
          echo "  ⚠️  File not found in worktree — creating instead"
          mkdir -p "$(dirname "$dest")"
        fi
        cp "$output_file" "$dest"
        echo "  ✅ Modified"
        files_staged+=("$task_file")
        ((applied++))
        ;;

      DELETE)
        if [[ -f "$output_file" && -s "$output_file" ]]; then
          # Stitched file with target removed
          cp "$output_file" "$dest"
          echo "  ✅ Target removed from file"
          files_staged+=("$task_file")
        elif [[ -f "$dest" ]]; then
          rm "$dest"
          echo "  ✅ File deleted"
          files_staged+=("$task_file")
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

    echo ""
  done

  echo "─────────────────────────────────────"
  echo "✅ Applied: $applied"
  [[ $skipped -gt 0 ]] && echo "⊘ Skipped: $skipped"
  [[ $failed -gt 0 ]] && echo "❌ Failed: $failed"
  echo ""

  # ── npm install if package.json was modified ─────────────────────────────
  local _pkg_modified=false
  for _f in "${files_staged[@]}"; do
    [[ "$_f" == "package.json" ]] && { _pkg_modified=true; break; }
  done
  if [[ "$_pkg_modified" == true ]]; then
    local _npm_cmd
    _npm_cmd=$(command -v npm 2>/dev/null || echo "")
    if [[ -n "$_npm_cmd" ]]; then
      echo "📦 package.json modified — running npm install..."
      (cd "$worktree_path" && "$_npm_cmd" install --silent 2>&1 | tail -3) || \
        echo "  ⚠️  npm install had warnings — check manually if needed"
      echo ""
    fi
  fi

  # ── TypeScript check in worktree ──────────────────────────────────────────
  if [[ -f "$worktree_path/tsconfig.json" ]] && \
     { command -v tsc &>/dev/null || [[ -f "$worktree_path/node_modules/.bin/tsc" ]]; }; then
    echo "🔍 Running tsc --noEmit..."
    local tsc_cmd="tsc"
    [[ -f "$worktree_path/node_modules/.bin/tsc" ]] && \
      tsc_cmd="$worktree_path/node_modules/.bin/tsc"
    local tsc_out
    tsc_out=$(cd "$worktree_path" && "$tsc_cmd" --noEmit --skipLibCheck 2>&1 || true)
    if [[ -z "$tsc_out" ]]; then
      echo "  ✅ TypeScript checks passed"
    else
      echo "  ⚠️  TypeScript errors found:"
      echo "$tsc_out" | head -20 | sed 's/^/     /'
      local tsc_count
      tsc_count=$(echo "$tsc_out" | grep -c "error TS" 2>/dev/null || echo "?")
      [[ $(echo "$tsc_out" | wc -l) -gt 20 ]] && \
        echo "     ... ($tsc_count error(s) total — see full output in worktree)"
      echo ""
      echo "  tsc errors are non-blocking — review before accepting"
    fi
    echo ""
  fi

  # ── Commit in worktree ────────────────────────────────────────────────────
  # Each worktree has its own index — no lock needed, concurrent workers are safe.
  (
    cd "$worktree_path" || exit 1
    for _f in "${files_staged[@]}"; do
      git add "$_f" 2>/dev/null || true
    done
    # Handle deletions: stage removed files
    git add -u 2>/dev/null || true
    git commit -m "agentic: $AGENTIC_SESSION"
  )

  echo ""
  echo "✅ Committed to $branch_name"
  echo ""
  echo "Review the changes, then:"
  echo "  Accept:  agentic accept $AGENTIC_SESSION"
  echo "  Reject:  agentic reject $AGENTIC_SESSION"
  echo "  Inspect: git diff HEAD..$branch_name"
  echo "  Worktree: $worktree_path"
  echo ""

  # ── Verify ────────────────────────────────────────────────────────────────
  if [[ $failed -eq 0 && $applied -gt 0 ]]; then
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
# verify-apply — check that worktree files match session outputs
# ─────────────────────────────────────────────────────────────────────────────

function verify-apply() {
  local session_dir
  session_dir=$(_apply_resolve_session)
  if [[ -z "$session_dir" ]]; then
    echo "❌ No active session."
    return 1
  fi
  export AGENTIC_SESSION="${AGENTIC_SESSION:-$(basename "${session_dir%/}")}"

  local tasks_file="$session_dir/tasks.json"
  [[ ! -f "$tasks_file" ]] && echo "❌ No tasks found." && return 1

  local worktree_path
  if [[ "${AGENTIC_APPLY_IN_PLACE:-}" == "1" ]]; then
    worktree_path="."
  else
    worktree_path=".claude/worktrees/${AGENTIC_SESSION}"
    if [[ ! -d "$worktree_path" ]]; then
      echo "❌ No worktree found at $worktree_path — run 'apply' first"
      return 1
    fi
  fi

  echo "🔍 Verifying applied changes in worktree..."
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
    local dest="$worktree_path/$task_file"
    local output_file="$session_dir/outputs/task_${task_id}.txt"

    echo "Task $task_id ($modification_type): $task_desc"
    echo "  File: $task_file"

    case "$task_action" in
      CREATE)
        if [[ -f "$dest" ]]; then
          if [[ -f "$output_file" ]] && diff -q "$dest" "$output_file" > /dev/null 2>&1; then
            echo "  ✅ Created and content matches"
          else
            echo "  ⚠️  File exists but content differs from generated output"
            ((mismatches++))
          fi
        else
          echo "  ❌ File was not created in worktree"
          ((mismatches++))
        fi
        ;;

      MODIFY)
        if [[ ! -f "$dest" ]]; then
          echo "  ❌ File does not exist in worktree: $task_file"
          ((mismatches++))
        else
          echo "  ✅ Modified"
        fi
        ;;

      DELETE)
        if [[ "$modification_type" == "delete_code" ]]; then
          if [[ -f "$dest" ]]; then
            echo "  ✅ Target removed from file"
          else
            echo "  ❌ File does not exist in worktree"
            ((mismatches++))
          fi
        else
          if [[ ! -f "$dest" ]]; then
            echo "  ✅ File deleted"
          else
            echo "  ❌ File still exists in worktree"
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

# ─────────────────────────────────────────────────────────────────────────────
# agentic-accept — merge the session's branch into the current branch and
# remove the worktree. Run this when you're happy with the generated changes.
# ─────────────────────────────────────────────────────────────────────────────

function agentic-accept() {
  local session_id="${1:-${AGENTIC_SESSION:-}}"
  if [[ -z "$session_id" ]]; then
    echo "❌ Usage: agentic accept SESSION_ID"
    echo "   Or set AGENTIC_SESSION and run: agentic accept"
    return 1
  fi

  local worktree_path=".claude/worktrees/$session_id"
  local branch_name="agentic/$session_id"
  local current_branch
  current_branch=$(git rev-parse --abbrev-ref HEAD)

  if ! git show-ref --verify --quiet "refs/heads/$branch_name"; then
    echo "❌ Branch $branch_name not found"
    return 1
  fi

  echo "🔀 Merging $branch_name → $current_branch"
  git merge "$branch_name" --no-ff -m "Accept agentic session: $session_id"

  if [[ -d "$worktree_path" ]]; then
    echo "🧹 Removing worktree..."
    git worktree remove "$worktree_path" --force
  fi

  echo ""
  echo "✅ Accepted: $session_id"
  echo "   Branch $branch_name merged into $current_branch"
  echo "   Run 'git push' when ready to share"
}

# ─────────────────────────────────────────────────────────────────────────────
# agentic-reject — discard the session's branch and remove the worktree.
# Run this when you don't want the generated changes.
# ─────────────────────────────────────────────────────────────────────────────

function agentic-reject() {
  local session_id="${1:-${AGENTIC_SESSION:-}}"
  if [[ -z "$session_id" ]]; then
    echo "❌ Usage: agentic reject SESSION_ID"
    echo "   Or set AGENTIC_SESSION and run: agentic reject"
    return 1
  fi

  local worktree_path=".claude/worktrees/$session_id"
  local branch_name="agentic/$session_id"

  if [[ -d "$worktree_path" ]]; then
    echo "🧹 Removing worktree..."
    git worktree remove "$worktree_path" --force
  fi

  if git show-ref --verify --quiet "refs/heads/$branch_name"; then
    git branch -D "$branch_name"
    echo "🗑  Deleted branch: $branch_name"
  fi

  echo ""
  echo "✅ Rejected: $session_id"
}

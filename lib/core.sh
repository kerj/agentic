#!/bin/bash
# Core orchestration

source "${AGENTIC_HOME}/lib/utils.sh"
source "${AGENTIC_HOME}/lib/config.sh"
source "${AGENTIC_HOME}/lib/init.sh"
source "${AGENTIC_HOME}/lib/doc.sh"
source "${AGENTIC_HOME}/lib/claude-api.sh"
source "${AGENTIC_HOME}/lib/architect.sh"
source "${AGENTIC_HOME}/lib/implement.sh"
source "${AGENTIC_HOME}/lib/validate.sh"
source "${AGENTIC_HOME}/lib/apply.sh"
source "${AGENTIC_HOME}/lib/refine.sh"
source "${AGENTIC_HOME}/lib/plan.sh"
source "${AGENTIC_HOME}/lib/metrics.sh"

# Sum real token counts from all usage sidecar files in a session
_core_sum_tokens() {
  local session_dir="$1"
  local total=0
  for f in "$session_dir"/outputs/*_usage.json "$session_dir"/*_usage.json; do
    [[ -f "$f" ]] || continue
    local t
    t=$(jq -r '(.input_tokens // 0) + (.output_tokens // 0) + (.cache_creation_input_tokens // 0) + (.cache_read_input_tokens // 0)' "$f" 2>/dev/null)
    total=$((total + ${t:-0}))
  done
  echo "$total"
}

function agentic() {
  echo "🚀 Agentic Workflow"
  echo "─────────────────────────────────────"
  echo ""

  local workflow_start
  workflow_start=$(date +%s)
  local iteration=1
  local max_iterations=5
  # Track whether the last repair pass actually reduced issue count.
  # If a repair plateaus, default the next failure's recommendation to refine
  # (the plan itself is probably wrong, not just the candidate code).
  local repair_plateaued=0
  # When set, the next loop iteration skips implement (repair already
  # updated the outputs in place; re-running implement would clobber them).
  local skip_implement_this_iter=0

  mkdir -p .claude/metrics
  local metrics_file=".claude/metrics/$(date +%Y%m%d-%H%M%S).json"
  init_metrics "$metrics_file"

  # ── Step 1: Architect ──────────────────────────────────────────────────────
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "Step 1: Planning"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""

  local step_start
  step_start=$(date +%s)

  if ! archie; then
    echo "❌ Planning failed"
    return 1
  fi

  local step_end
  step_end=$(date +%s)
  local plan_duration=$(( step_end - step_start ))

  # Resolve session dir now that archie has set AGENTIC_SESSION
  local session_dir
  session_dir=$(_apply_resolve_session)
  if [[ -z "$session_dir" ]]; then
    echo "❌ Could not resolve session after planning"
    return 1
  fi

  local plan_tokens
  plan_tokens=$(jq -r '(.input_tokens // 0) + (.output_tokens // 0)' \
    "$session_dir/architect_usage.json" 2>/dev/null || echo 0)

  echo ""
  echo "⏱️  Planning: ${plan_duration}s | tokens: $plan_tokens"
  echo ""

  log_step_metrics "$metrics_file" "architect" "$plan_duration" "$plan_tokens" "success"

  # ── Implement → Validate → Refine loop ────────────────────────────────────
  while [[ $iteration -le $max_iterations ]]; do
    if [[ $skip_implement_this_iter -eq 1 ]]; then
      skip_implement_this_iter=0
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      echo "Step 2: Implementation (skipped — repair updated outputs in place)"
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      echo ""
    else
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Step 2: Implementation (Iteration $iteration)"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    step_start=$(date +%s)
    export SKIP_APPLY_PROMPT=1

    if ! implement; then
      unset SKIP_APPLY_PROMPT
      echo "❌ Implementation failed"
      finalize_metrics "$metrics_file" "$workflow_start" 0 "implement_failed"
      return 1
    fi

    unset SKIP_APPLY_PROMPT
    step_end=$(date +%s)
    local impl_duration=$(( step_end - step_start ))
    local impl_tokens
    impl_tokens=$(_core_sum_tokens "$session_dir")

    echo ""
    echo "⏱️  Implementation: ${impl_duration}s | tokens: $impl_tokens"
    echo ""

    log_step_metrics "$metrics_file" "implement_${iteration}" \
      "$impl_duration" "$impl_tokens" "success"
    fi

    # ── Check validation result ──────────────────────────────────────────────
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Step 3: Validation result"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # ── AI review (runs only when static validation passes) ───────────────────
    if [[ ! -f "$session_dir/validation_issues.txt" || \
          ! -s "$session_dir/validation_issues.txt" ]]; then
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      echo "Step 3b: AI Review"
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      echo ""

      local review_start review_end
      review_start=$(date +%s)
      review || true   # review() appends to validation_issues.txt on REJECTED
      review_end=$(date +%s)
      local review_tokens
      review_tokens=$(jq -r '(.input_tokens // 0) + (.output_tokens // 0)' \
        "$session_dir/review_usage.json" 2>/dev/null || echo 0)
      echo "⏱️  Review: $(( review_end - review_start ))s | tokens: $review_tokens"
      echo ""
      log_step_metrics "$metrics_file" "review_${iteration}" \
        "$(( review_end - review_start ))" "$review_tokens" "success"
    fi

    if [[ -f "$session_dir/validation_issues.txt" && \
          -s "$session_dir/validation_issues.txt" ]]; then
      local issue_count
      issue_count=$(wc -l < "$session_dir/validation_issues.txt" | tr -d ' ')
      echo "❌ Validation found $issue_count issue(s)"
      echo ""

      log_step_metrics "$metrics_file" "validate_${iteration}" 0 0 "failed"

      if [[ $iteration -ge $max_iterations ]]; then
        echo "⚠️  Max iterations ($max_iterations) reached"
        echo ""
        read -p "Apply anyway? (y/n) " force_apply
        if [[ "$force_apply" =~ ^[Yy]$ ]]; then
          break
        else
          echo "Workflow stopped. Run 'refine' manually."
          finalize_metrics "$metrics_file" "$workflow_start" \
            "$(_core_sum_tokens "$session_dir")" "stopped"
          return 1
        fi
      fi

      # Pick the recommended remediation:
      #   - First failure (or after a productive repair): try repair (cheap, targeted)
      #   - After a repair that didn't reduce issue count: refine (replan)
      local recommended="repair"
      [[ $repair_plateaued -eq 1 ]] && recommended="refine"

      echo "Options:"
      if [[ "$recommended" == "repair" ]]; then
        echo "  1. Repair failing tasks in place (recommended — keeps the plan)"
        echo "  2. Refine — throw out the plan and replan from scratch"
      else
        echo "  1. Refine — throw out the plan and replan from scratch (recommended)"
        echo "  2. Repair failing tasks in place (last repair didn't help)"
      fi
      echo "  3. Apply anyway"
      echo "  4. Stop"
      echo ""
      read -p "Choice (1/2/3/4): " refine_choice

      # Normalize: map the user's 1/2 onto the actual action based on which
      # one is recommended this round.
      local action=""
      case "$refine_choice" in
        1) [[ "$recommended" == "repair" ]] && action="repair" || action="refine" ;;
        2) [[ "$recommended" == "repair" ]] && action="refine" || action="repair" ;;
        3) action="apply_anyway" ;;
        4) action="stop" ;;
        *) action="stop" ;;
      esac

      case "$action" in
        repair)
          echo ""
          echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
          echo "Step 4: Repair (Iteration $iteration)"
          echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
          echo ""

          local issues_before=$issue_count
          local tokens_before
          tokens_before=$(_core_sum_tokens "$session_dir")

          step_start=$(date +%s)
          export SKIP_APPLY_PROMPT=1

          local repair_rc=0
          repair || repair_rc=$?

          unset SKIP_APPLY_PROMPT
          step_end=$(date +%s)
          local repair_duration=$(( step_end - step_start ))
          local tokens_after
          tokens_after=$(_core_sum_tokens "$session_dir")
          local repair_tokens=$(( tokens_after - tokens_before ))

          echo ""
          echo "⏱️  Repair: ${repair_duration}s | tokens: $repair_tokens"
          echo ""

          # Did it help? repair re-runs validate at its end, so the issues
          # file now reflects the post-repair state.
          local issues_after=0
          if [[ -f "$session_dir/validation_issues.txt" && \
                -s "$session_dir/validation_issues.txt" ]]; then
            issues_after=$(wc -l < "$session_dir/validation_issues.txt" | tr -d ' ')
          fi

          if [[ $repair_rc -eq 0 ]]; then
            log_step_metrics "$metrics_file" "repair_${iteration}" \
              "$repair_duration" "$repair_tokens" "success"
            repair_plateaued=0
            # repair succeeded — validation_issues.txt is gone, loop will
            # detect "passed" on next iteration through the check below.
          else
            log_step_metrics "$metrics_file" "repair_${iteration}" \
              "$repair_duration" "$repair_tokens" "partial"
            if [[ $issues_after -ge $issues_before ]]; then
              echo "⚠️  Repair did not reduce issue count ($issues_before → $issues_after)."
              echo "    Will recommend 'refine' on next prompt."
              repair_plateaued=1
            else
              echo "✓ Repair reduced issues from $issues_before to $issues_after."
              repair_plateaued=0
            fi
          fi

          ((iteration++))
          # Repair updated outputs in place — skip implement on the next
          # loop iteration and go straight to the validation check.
          skip_implement_this_iter=1
          continue
          ;;

        refine)
          echo ""
          echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
          echo "Step 4: Refine (Iteration $iteration)"
          echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
          echo ""

          step_start=$(date +%s)
          export SKIP_IMPLEMENT_PROMPT=1

          if ! refine; then
            unset SKIP_IMPLEMENT_PROMPT
            echo "❌ Refine failed"
            finalize_metrics "$metrics_file" "$workflow_start" \
              "$(_core_sum_tokens "$session_dir")" "failed"
            return 1
          fi

          unset SKIP_IMPLEMENT_PROMPT
          step_end=$(date +%s)
          local refine_duration=$(( step_end - step_start ))
          local refine_tokens
          refine_tokens=$(jq -r '(.input_tokens // 0) + (.output_tokens // 0)' \
            "$session_dir/refine_iteration_${iteration}_usage.json" 2>/dev/null || echo 0)

          echo ""
          echo "⏱️  Refine: ${refine_duration}s | tokens: $refine_tokens"
          echo ""

          log_step_metrics "$metrics_file" "refine_${iteration}" \
            "$refine_duration" "$refine_tokens" "success"

          # Refine wipes outputs/ — fresh implement run will follow
          repair_plateaued=0
          ((iteration++))
          continue
          ;;

        apply_anyway)
          echo "⚠️  Proceeding with issues..."
          log_step_metrics "$metrics_file" "validate_${iteration}" 0 0 "warning"
          break
          ;;

        stop)
          echo "Workflow stopped."
          finalize_metrics "$metrics_file" "$workflow_start" \
            "$(_core_sum_tokens "$session_dir")" "manual_stop"
          return 0
          ;;
      esac
    else
      echo "✅ Validation passed"
      log_step_metrics "$metrics_file" "validate_${iteration}" 0 0 "success"
      break
    fi
  done

  # ── Step 5: Apply ──────────────────────────────────────────────────────────
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "Step 5: Apply Changes"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""

  step_start=$(date +%s)

  if ! apply; then
    echo "❌ Apply failed"
    finalize_metrics "$metrics_file" "$workflow_start" \
      "$(_core_sum_tokens "$session_dir")" "apply_failed"
    return 1
  fi

  step_end=$(date +%s)
  local apply_duration=$(( step_end - step_start ))

  log_step_metrics "$metrics_file" "apply" "$apply_duration" 0 "success"

  # ── Complete ───────────────────────────────────────────────────────────────
  local workflow_end
  workflow_end=$(date +%s)
  local total_duration=$(( workflow_end - workflow_start ))
  local total_tokens
  total_tokens=$(_core_sum_tokens "$session_dir")

  finalize_metrics "$metrics_file" "$workflow_start" "$total_tokens" "success"

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "✅ Workflow Complete!"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  echo "📊 Session:"
  echo "   Duration:   $(format_duration $total_duration)"
  echo "   Tokens:     $total_tokens"
  echo "   Iterations: $iteration"
  echo "   Session:    $AGENTIC_SESSION"
  echo ""
  echo "📝 Metrics: $metrics_file"
  echo ""
}
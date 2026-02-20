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

    # ── Check validation result ──────────────────────────────────────────────
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Step 3: Validation result"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

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

      echo "Options:"
      echo "  1. Refine and try again (recommended)"
      echo "  2. Apply anyway"
      echo "  3. Stop"
      echo ""
      read -p "Choice (1/2/3): " refine_choice

      case "$refine_choice" in
        1)
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

          ((iteration++))
          continue
          ;;
        2)
          echo "⚠️  Proceeding with issues..."
          log_step_metrics "$metrics_file" "validate_${iteration}" 0 0 "warning"
          break
          ;;
        3)
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
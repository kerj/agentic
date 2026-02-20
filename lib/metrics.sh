#!/bin/bash
# Metrics functions

log_step_metrics() {
  local metrics_file="$1"
  local step="$2"
  local duration="$3"
  local tokens="$4"
  local status="$5"

  [[ ! -f "$metrics_file" ]] && return

  local step_entry
  step_entry=$(jq -n \
    --arg step "$step" \
    --argjson duration "$duration" \
    --argjson tokens "$tokens" \
    --arg status "$status" \
    --arg timestamp "$(date -Iseconds)" \
    '{step: $step, duration_seconds: $duration, tokens: $tokens, status: $status, timestamp: $timestamp}')

  # Append to steps array using a temp file
  local tmp="${metrics_file}.tmp"
  jq --argjson entry "$step_entry" '.steps += [$entry]' "$metrics_file" > "$tmp" \
    && mv "$tmp" "$metrics_file"
}

finalize_metrics() {
  local metrics_file="$1"
  local workflow_start="$2"
  local total_tokens="$3"
  local status="$4"

  [[ ! -f "$metrics_file" ]] && return

  local workflow_end
  workflow_end=$(date +%s)
  local total_duration=$(( workflow_end - workflow_start ))

  local tmp="${metrics_file}.tmp"
  jq \
    --arg status "$status" \
    --argjson duration "$total_duration" \
    --argjson tokens "$total_tokens" \
    --arg session "${AGENTIC_SESSION:-unknown}" \
    --arg end_time "$(date -Iseconds)" \
    '.status = $status |
     .total_duration_seconds = $duration |
     .total_tokens = $tokens |
     .session = $session |
     .end_time = $end_time' \
    "$metrics_file" > "$tmp" && mv "$tmp" "$metrics_file"
}

function agentic-metrics() {
  local metrics_dir=".claude/metrics"
  local metric_file="${1:-}"

  if [[ -z "$metric_file" ]]; then
    if [[ ! -d "$metrics_dir" ]] || [[ -z "$(ls -A "$metrics_dir" 2>/dev/null)" ]]; then
      echo "❌ No metrics files found in $metrics_dir"
      return 1
    fi
    metric_file="$metrics_dir/$(ls -t "$metrics_dir" | head -1)"
  fi

  if [[ ! -f "$metric_file" ]]; then
    echo "❌ Metrics file not found: $metric_file"
    echo "Usage: agentic-metrics [path/to/metrics.json]"
    return 1
  fi

  echo "📊 Metrics Report"
  echo "─────────────────────────────────────"
  echo ""

  local session status duration tokens
  session=$(jq -r '.session // "unknown"' "$metric_file")
  status=$(jq -r '.status // "unknown"' "$metric_file")
  duration=$(jq -r '.total_duration_seconds // 0' "$metric_file")
  tokens=$(jq -r '.total_tokens // 0' "$metric_file")

  echo "Session:  $session"
  echo "Status:   $status"
  echo "Duration: $(format_duration "$duration")"
  echo "Tokens:   $tokens"
  echo ""
  echo "Steps:"
  jq -r '.steps[]? |
    "  \(.step): \(.duration_seconds)s | \(.tokens) tokens | \(.status)"' \
    "$metric_file"
  echo ""
  echo "File: $metric_file"
}

# Initialise a new metrics file for a session
init_metrics() {
  local metrics_file="$1"
  jq -n \
    --arg start_time "$(date -Iseconds)" \
    --arg session "${AGENTIC_SESSION:-unknown}" \
    '{start_time: $start_time, session: $session, status: "running", steps: []}' \
    > "$metrics_file"
}
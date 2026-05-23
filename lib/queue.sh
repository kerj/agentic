#!/bin/bash
# File-based job queue primitives for agentic worker scheduling.
#
# Filesystem layout (~/.agentic/queue/):
#   pending/    — submitted, not yet claimed
#   running/    — claimed by a worker, in progress
#   done/       — completed successfully
#   failed/     — completed with failure
#   cancelled/  — cancelled before claim
#
# Worktrees: ~/.agentic/worktrees/<job-id>/
#
# Job file naming: {priority}_{YYYYMMDD}_{HHMMSS}_{id}.json
#   Sorted by: sort -t_ -k1,1rn -k2,3 → highest priority first, FIFO within priority.
#
# Job file schema:
#   id:                j_YYYYMMDD_HHMMSS_xxxx
#   request:           natural-language task description
#   target_repo:       absolute path to the git repo
#   model_hint:        "local" | "remote" | "auto"  (Stage 8 reads this; Stage 5 ignores)
#   priority:          integer, default 0, higher = claimed first
#   parent_request_id: null in Stage 5; Stage 11's dispatcher populates it
#   submitted_at:      ISO-8601 UTC timestamp
#   submitted_by:      hostname:PID — used for stale-lock recovery in Stage 6+
#   state_history:     append-only [{state, at}] — makes post-mortem debugging easy
#   summary:           (optional) one-line result, added by queue_complete on completion

AGENTIC_QUEUE_DIR="${AGENTIC_HOME}/queue"
AGENTIC_WORKTREES_DIR="${AGENTIC_HOME}/worktrees"

# _queue_init — creates the queue and worktrees directories if absent. Idempotent.
_queue_init() {
  mkdir -p \
    "${AGENTIC_QUEUE_DIR}/pending" \
    "${AGENTIC_QUEUE_DIR}/running" \
    "${AGENTIC_QUEUE_DIR}/done" \
    "${AGENTIC_QUEUE_DIR}/failed" \
    "${AGENTIC_QUEUE_DIR}/abandoned" \
    "${AGENTIC_QUEUE_DIR}/cancelled" \
    "${AGENTIC_WORKTREES_DIR}"
}

# _queue_new_id — returns j_YYYYMMDD_HHMMSS_xxxx. One place to change if format evolves.
_queue_new_id() {
  local ts
  ts=$(date +%Y%m%d_%H%M%S)
  local rand
  rand=$(openssl rand -hex 2 2>/dev/null \
    || head -c 2 /dev/urandom | xxd -p 2>/dev/null \
    || printf '%04x' $(( (RANDOM * 31337 + RANDOM) % 65536 )))
  echo "j_${ts}_${rand}"
}

# queue_submit REQUEST TARGET_REPO [MODEL_HINT] [PRIORITY] [PARENT_REQUEST_ID]
# Validates target_repo, writes a job file to pending/, echoes the job ID on stdout.
# Returns 1 with a stderr message on validation failure.
queue_submit() {
  local request="$1"
  local target_repo="$2"
  local model_hint="${3:-auto}"
  local priority="${4:-0}"
  local parent_request_id="${5:-}"

  _queue_init

  if [[ -z "$target_repo" || ! -d "$target_repo" ]]; then
    echo "queue_submit: target_repo does not exist: ${target_repo}" >&2
    return 1
  fi
  if ! git -C "$target_repo" rev-parse --git-dir > /dev/null 2>&1; then
    echo "queue_submit: not a git repository: ${target_repo}" >&2
    return 1
  fi

  local id
  id=$(_queue_new_id)
  local now
  now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  local submitted_by
  submitted_by="$(hostname -s 2>/dev/null || hostname):$$"
  local base_branch
  base_branch=$(git -C "$target_repo" symbolic-ref --short HEAD 2>/dev/null || echo "HEAD")

  # Extract YYYYMMDD_HHMMSS from id for the filename prefix (enables ls-based claim order)
  local ts="${id#j_}"
  ts="${ts%_*}"
  local filename="${priority}_${ts}_${id}.json"

  jq -n \
    --arg id "$id" \
    --arg request "$request" \
    --arg target_repo "$target_repo" \
    --arg model_hint "$model_hint" \
    --argjson priority "$priority" \
    --arg parent_request_id "$parent_request_id" \
    --arg submitted_at "$now" \
    --arg submitted_by "$submitted_by" \
    --arg base_branch "$base_branch" \
    '{
      id: $id,
      request: $request,
      target_repo: $target_repo,
      model_hint: $model_hint,
      priority: $priority,
      base_branch: $base_branch,
      parent_request_id: (if $parent_request_id == "" then null else $parent_request_id end),
      submitted_at: $submitted_at,
      submitted_by: $submitted_by,
      state_history: [{state: "pending", at: $submitted_at}]
    }' > "${AGENTIC_QUEUE_DIR}/pending/${filename}"

  echo "$id"
}

# queue_claim — atomically claims the highest-priority oldest job from pending/.
# Skips any job whose parent_request_id is still in pending/ or running/ so that
# chained jobs never execute before their parent has completed.
# Echoes the path to the now-running/ job file on stdout.
# Returns 1 if nothing claimable is pending.
queue_claim() {
  _queue_init

  local worker_id
  worker_id="$(hostname -s 2>/dev/null || hostname):$$"
  local now
  now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  # sort: highest priority (k1 reverse numeric) first, then oldest timestamp (k2,k3 ascending)
  local candidate
  for candidate in $(ls "${AGENTIC_QUEUE_DIR}/pending/" 2>/dev/null \
      | grep '\.json$' \
      | sort -t_ -k1,1rn -k2,3); do
    local src="${AGENTIC_QUEUE_DIR}/pending/${candidate}"
    local dst="${AGENTIC_QUEUE_DIR}/running/${candidate}"

    # Chain-dependency check: skip if the parent job is still pending or running
    local parent_id
    parent_id=$(jq -r '.parent_request_id // empty' "$src" 2>/dev/null)
    if [[ -n "$parent_id" ]]; then
      if grep -rl "\"id\": \"${parent_id}\"" \
           "${AGENTIC_QUEUE_DIR}/pending" \
           "${AGENTIC_QUEUE_DIR}/running" 2>/dev/null | grep -q .; then
        continue
      fi
    fi

    # Atomic rename — first claimer wins; ENOENT from a concurrent claimer is silently skipped
    if mv "$src" "$dst" 2>/dev/null; then
      local updated
      updated=$(jq \
        --arg state "claimed" \
        --arg at "$now" \
        --arg worker "$worker_id" \
        '.state_history += [{state: $state, at: $at, worker: $worker}]' \
        "$dst")
      echo "$updated" > "$dst"
      echo "$dst"
      return 0
    fi
  done

  return 1
}

# queue_complete JOB_PATH STATUS [SUMMARY]
# STATUS: done | failed | cancelled
# Appends a terminal state_history entry, optionally stores a summary,
# and moves the file to ~/.agentic/queue/{STATUS}/.
queue_complete() {
  local job_path="$1"
  local status="$2"
  local summary="${3:-}"

  _queue_init

  local now
  now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  local updated
  if [[ -n "$summary" ]]; then
    updated=$(jq \
      --arg state "$status" \
      --arg at "$now" \
      --arg summary "$summary" \
      '.state_history += [{state: $state, at: $at}] | .summary = $summary' \
      "$job_path")
  else
    updated=$(jq \
      --arg state "$status" \
      --arg at "$now" \
      '.state_history += [{state: $state, at: $at}]' \
      "$job_path")
  fi
  echo "$updated" > "$job_path"

  local filename
  filename=$(basename "$job_path")
  mv "$job_path" "${AGENTIC_QUEUE_DIR}/${status}/${filename}"
}

# _queue_notify TITLE MESSAGE — cross-platform desktop notification, silent on unknown platforms.
_queue_notify() {
  local title="$1"
  local message="$2"
  # Escape for AppleScript string literals
  local safe_title="${title//\\/\\\\}"
  safe_title="${safe_title//\"/\\\"}"
  local safe_msg="${message//\\/\\\\}"
  safe_msg="${safe_msg//\"/\\\"}"
  case "$(uname -s)" in
    Darwin)
      osascript -e "display notification \"${safe_msg}\" with title \"${safe_title}\"" 2>/dev/null || true
      ;;
    Linux)
      command -v notify-send &>/dev/null && notify-send "$title" "$message" 2>/dev/null || true
      ;;
    *)
      ;;
  esac
}

# _queue_relative_time ISO8601_UTC — returns "3m ago", "2h ago", etc.
_queue_relative_time() {
  local ts="$1"
  local now_epoch
  now_epoch=$(date +%s)
  local then_epoch
  # Try GNU date (-d), then BSD date (-j -f)
  then_epoch=$(date -u -d "$ts" +%s 2>/dev/null \
    || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$ts" +%s 2>/dev/null \
    || echo "$now_epoch")
  local diff=$(( now_epoch - then_epoch ))
  if   [[ $diff -lt 60 ]];    then echo "${diff}s ago"
  elif [[ $diff -lt 3600 ]];  then echo "$(( diff / 60 ))m ago"
  elif [[ $diff -lt 86400 ]]; then echo "$(( diff / 3600 ))h ago"
  else                              echo "$(( diff / 86400 ))d ago"
  fi
}

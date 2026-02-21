#!/bin/bash
# lib/claude-api.sh
#
# Utility for calling the Anthropic Messages API directly.
# Handles JSON escaping, prompt caching, retries, and response parsing.
#
# Usage:
#   source "$AGENTIC_HOME/lib/claude-api.sh"
#
#   claude_api \
#     --model        "claude-sonnet-4-6"   \
#     --system       "$system_prompt"       \   # optional; cached if --cache-system set
#     --cache-system                         \   # flag: add cache_control to system prompt
#     --user         "$user_prompt"          \
#     --output       "/path/to/output.txt"   \   # writes extracted text here
#     --usage        "/path/to/usage.json"   \   # optional: writes token counts here
#     --max-tokens   4096                    \   # default: 8192
#     --timeout      120                         # curl timeout in seconds, default: 180

# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

# Detect whether we are talking to the real Anthropic API or a local proxy
# (Ollama, LiteLLM, etc.). Caching headers are Anthropic-only.
_claude_api_is_anthropic() {
  local base_url="${ANTHROPIC_BASE_URL:-https://api.anthropic.com}"
  [[ "$base_url" == *"anthropic.com"* ]]
}

# Resolve the endpoint URL
_claude_api_endpoint() {
  local base="${ANTHROPIC_BASE_URL:-https://api.anthropic.com}"
  # Normalise: strip trailing slash
  base="${base%/}"
  echo "${base}/v1/messages"
}

# Resolve the API key
_claude_api_key() {
  # Prefer ANTHROPIC_API_KEY; fall back to ANTHROPIC_AUTH_TOKEN (Ollama default)
  echo "${ANTHROPIC_API_KEY:-${ANTHROPIC_AUTH_TOKEN:-ollama}}"
}

# Build the system array as a JSON array string.
# If cache=true, the last block gets cache_control injected.
_claude_api_build_system_json() {
  local system_text="$1"
  local cache="$2"     # "true" or "false"

  if [[ -z "$system_text" ]]; then
    echo "null"
    return
  fi

  if [[ "$cache" == "true" ]] && _claude_api_is_anthropic; then
    # Single block with cache_control
    jq -n \
      --arg text "$system_text" \
      '[{type: "text", text: $text, cache_control: {type: "ephemeral"}}]'
  else
    jq -n \
      --arg text "$system_text" \
      '[{type: "text", text: $text}]'
  fi
}

# Build the full request payload as JSON
_claude_api_build_payload() {
  local model="$1"
  local max_tokens="$2"
  local system_json="$3"   # already-valid JSON (array or null)
  local user_text="$4"

  if [[ "$system_json" == "null" ]]; then
    jq -n \
      --arg   model      "$model"      \
      --argjson max_tokens "$max_tokens" \
      --arg   user       "$user_text"  \
      '{
        model:      $model,
        max_tokens: $max_tokens,
        messages: [{role: "user", content: $user}]
      }'
  else
    jq -n \
      --arg    model       "$model"      \
      --argjson max_tokens  "$max_tokens" \
      --argjson system      "$system_json" \
      --arg    user        "$user_text"  \
      '{
        model:      $model,
        max_tokens: $max_tokens,
        system:     $system,
        messages:   [{role: "user", content: $user}]
      }'
  fi
}

# Extract the assistant text from a successful response JSON.
# The content array may contain multiple blocks; we join all text blocks.
_claude_api_extract_text() {
  local response_json="$1"
  jq -r '[.content[] | select(.type == "text") | .text] | join("")' \
    <<< "$response_json"
}

# Extract usage statistics from a successful response JSON.
_claude_api_extract_usage() {
  local response_json="$1"
  jq -r '{
    input_tokens:               (.usage.input_tokens          // 0),
    output_tokens:              (.usage.output_tokens         // 0),
    cache_creation_input_tokens:(.usage.cache_creation_input_tokens // 0),
    cache_read_input_tokens:    (.usage.cache_read_input_tokens    // 0)
  }' <<< "$response_json"
}

# ─────────────────────────────────────────────────────────────────────────────
# Retry loop
# ─────────────────────────────────────────────────────────────────────────────

# Calls curl once. Writes raw HTTP status to stdout; response body to $tmpfile.
# Returns 0 on any completed HTTP exchange, 1 on curl error (network/timeout).
_claude_api_curl_once() {
  local endpoint="$1"
  local api_key="$2"
  local payload_file="$3"
  local response_file="$4"
  local timeout="$5"
  local cache="$6"

  local -a headers=(
    -H "x-api-key: $api_key"
    -H "anthropic-version: 2023-06-01"
    -H "content-type: application/json"
  )

  # Only send the caching beta header to Anthropic's own endpoint
  if [[ "$cache" == "true" ]] && _claude_api_is_anthropic; then
    headers+=(-H "anthropic-beta: prompt-caching-2024-07-31")
  fi

  local http_status
  # http_status=$(curl -s -v -w "%{http_code}" \
  #   --max-time "$timeout" \
  #   "${headers[@]}" \
  #   -d "@$payload_file" \
  #   -o "$response_file" \
  #   "$endpoint" 2>/dev/null)
  http_status=$(curl -s -v -w "%{http_code}" \
  --max-time "$timeout" \
  "${headers[@]}" \
  -d "@$payload_file" \
  -o "$response_file" \
  "$endpoint" 2>&1 | tee /tmp/curl_debug.txt | tail -1)

  local curl_exit=$?

  if [[ $curl_exit -ne 0 ]]; then
    echo "CURL_ERROR:$curl_exit"
    return 1
  fi

  echo "$http_status"
  return 0
}

# ─────────────────────────────────────────────────────────────────────────────
# Public interface
# ─────────────────────────────────────────────────────────────────────────────

claude_api() {
  # ── Defaults ──────────────────────────────────────────────────────────────
  local model="${AGENTIC_MODEL:-claude-sonnet-4-6}"
  local max_tokens=8192
  local timeout=180
  local system_text=""
  local cache_system="false"
  local user_text=""
  local output_file=""
  local usage_file=""

  # ── Argument parsing ──────────────────────────────────────────────────────
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --model)        model="$2";        shift 2 ;;
      --system)       system_text="$2";  shift 2 ;;
      --cache-system) cache_system="true"; shift ;;
      --user)         user_text="$2";    shift 2 ;;
      --output)       output_file="$2";  shift 2 ;;
      --usage)        usage_file="$2";   shift 2 ;;
      --max-tokens)   max_tokens="$2";   shift 2 ;;
      --timeout)      timeout="$2";      shift 2 ;;
      *)
        echo "claude_api: unknown argument: $1" >&2
        return 1
        ;;
    esac
  done

  # ── Validate required args ────────────────────────────────────────────────
  if [[ -z "$user_text" ]]; then
    echo "claude_api: --user is required" >&2
    return 1
  fi
  if [[ -z "$output_file" ]]; then
    echo "claude_api: --output is required" >&2
    return 1
  fi

  # ── Ensure output directory exists ────────────────────────────────────────
  mkdir -p "$(dirname "$output_file")"

  # ── Build request ─────────────────────────────────────────────────────────
  local endpoint
  endpoint=$(_claude_api_endpoint)

  local api_key
  api_key=$(_claude_api_key)

  local system_json
  system_json=$(_claude_api_build_system_json "$system_text" "$cache_system")

  local payload
  payload=$(_claude_api_build_payload "$model" "$max_tokens" "$system_json" "$user_text")

  if [[ -z "$payload" ]]; then
    echo "claude_api: failed to build request payload (jq error?)" >&2
    return 1
  fi

  # Write payload to temp file so curl reads it cleanly (avoids shell escaping issues)
  local tmp_payload tmp_response tmp_dir
  tmp_dir=$(mktemp -d)
  tmp_payload="$tmp_dir/payload.json"
  tmp_response="$tmp_dir/response.json"
  echo "$payload" > "$tmp_payload"

  # echo "DEBUG payload:" >&2
  # cat "$tmp_payload" >&2

  # ── Retry loop ────────────────────────────────────────────────────────────
  local max_retries=4
  local attempt=0
  local http_status
  local backoff=2

  while [[ $attempt -lt $max_retries ]]; do
    ((attempt++))

    http_status=$(_claude_api_curl_once \
      "$endpoint" "$api_key" "$tmp_payload" "$tmp_response" "$timeout" "$cache_system")

    local curl_result=$?

    # curl-level failure (network, timeout)
    if [[ $curl_result -ne 0 ]]; then
      local curl_code="${http_status#CURL_ERROR:}"
      echo "  ⚠️  curl error (code $curl_code), attempt $attempt/$max_retries" >&2

      if [[ $attempt -lt $max_retries ]]; then
        echo "  ↩️  Retrying in ${backoff}s..." >&2
        sleep $backoff
        backoff=$((backoff * 2))
        continue
      fi

      echo "claude_api: curl failed after $max_retries attempts (code $curl_code)" >&2
      rm -rf "$tmp_dir"
      return 1
    fi

    # ── HTTP-level response handling ──────────────────────────────────────

    case "$http_status" in

      200)
        # Success — validate it looks like a proper response before accepting
        if ! jq -e '.content' "$tmp_response" &>/dev/null; then
          echo "claude_api: 200 response but no .content field — malformed response" >&2
          echo "  Raw response:" >&2
          cat "$tmp_response" >&2
          rm -rf "$tmp_dir"
          return 1
        fi

        # Check for API-level error embedded in a 200 (shouldn't happen but does occasionally)
        local resp_type
        resp_type=$(jq -r '.type // "message"' "$tmp_response")
        if [[ "$resp_type" == "error" ]]; then
          local err_msg
          err_msg=$(jq -r '.error.message // "unknown error"' "$tmp_response")
          echo "claude_api: API error in 200 response: $err_msg" >&2
          rm -rf "$tmp_dir"
          return 1
        fi

        # Extract text to output file
        _claude_api_extract_text "$(cat "$tmp_response")" > "$output_file"

        # Check output is non-empty
        if [[ ! -s "$output_file" ]]; then
          echo "claude_api: empty response text extracted" >&2
          echo "  stop_reason: $(jq -r '.stop_reason // "unknown"' "$tmp_response")" >&2
          rm -rf "$tmp_dir"
          return 1
        fi

        # Write usage sidecar if requested
        if [[ -n "$usage_file" ]]; then
          mkdir -p "$(dirname "$usage_file")"
          _claude_api_extract_usage "$(cat "$tmp_response")" > "$usage_file"
        fi

        rm -rf "$tmp_dir"
        return 0
        ;;

      429|529)
        # Rate limited or overloaded — retry with backoff
        local retry_after
        retry_after=$(jq -r '.error.message // ""' "$tmp_response" | grep -oE '[0-9]+' | head -1)
        local wait=${retry_after:-$backoff}
        echo "  ⚠️  HTTP $http_status (rate limit / overloaded), attempt $attempt/$max_retries" >&2

        if [[ $attempt -lt $max_retries ]]; then
          echo "  ↩️  Retrying in ${wait}s..." >&2
          sleep $wait
          backoff=$((backoff * 2))
          continue
        fi

        echo "claude_api: rate limit/overload after $max_retries attempts" >&2
        rm -rf "$tmp_dir"
        return 1
        ;;

      400|401|403|404)
        # Non-retryable client errors
        local err_msg
        err_msg=$(jq -r '.error.message // "unknown error"' "$tmp_response" 2>/dev/null)
        echo "claude_api: HTTP $http_status — $err_msg" >&2
        rm -rf "$tmp_dir"
        return 1
        ;;

      500|502|503|504)
        # Server errors — retry
        echo "  ⚠️  HTTP $http_status (server error), attempt $attempt/$max_retries" >&2

        if [[ $attempt -lt $max_retries ]]; then
          echo "  ↩️  Retrying in ${backoff}s..." >&2
          sleep $backoff
          backoff=$((backoff * 2))
          continue
        fi

        echo "claude_api: server error after $max_retries attempts (HTTP $http_status)" >&2
        rm -rf "$tmp_dir"
        return 1
        ;;

      *)
        # Unexpected status
        echo "claude_api: unexpected HTTP status $http_status" >&2
        echo "  Response body:" >&2
        cat "$tmp_response" >&2
        rm -rf "$tmp_dir"
        return 1
        ;;

    esac
  done

  # Should not reach here
  rm -rf "$tmp_dir"
  echo "claude_api: exhausted retries" >&2
  return 1
}

# ─────────────────────────────────────────────────────────────────────────────
# Convenience: sum usage across multiple sidecar files into one total
# ─────────────────────────────────────────────────────────────────────────────

claude_api_sum_usage() {
  # Usage: claude_api_sum_usage file1.json file2.json ...
  # Prints a JSON object with summed token counts
  local files=("$@")

  if [[ ${#files[@]} -eq 0 ]]; then
    echo '{"input_tokens":0,"output_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}'
    return
  fi

  # Feed all files as a JSON array and sum each field
  jq -s '{
    input_tokens:                (map(.input_tokens)                | add // 0),
    output_tokens:               (map(.output_tokens)               | add // 0),
    cache_creation_input_tokens: (map(.cache_creation_input_tokens) | add // 0),
    cache_read_input_tokens:     (map(.cache_read_input_tokens)     | add // 0)
  }' "${files[@]}"
}
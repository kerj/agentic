#!/bin/bash
# Utility functions — shared across all agentic scripts

# Slugify a string for use in session names
_session_slug() {
  echo "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed 's/[^a-z0-9]/-/g' \
    | sed 's/--*/-/g' \
    | cut -c1-30
}

# Format seconds as "Xm Ys" or "Ys"
format_duration() {
  local secs="${1:-0}"
  local mins=$(( secs / 60 ))
  local rem=$(( secs % 60 ))
  [[ $mins -gt 0 ]] && echo "${mins}m ${rem}s" || echo "${rem}s"
}

# Clean absolute or cwd-relative path prefixes from a file path
_apply_clean_path() {
  local path="$1"
  path="${path#$(pwd)/}"
  echo "$path"
}

# Resolve active session dir — returns empty string if none found.
# Primary path: AGENTIC_SESSION env var (must be set by workers explicitly).
# Fallback: most recently modified session dir (interactive shell convenience only —
# not safe under concurrent workers; workers must always supply AGENTIC_SESSION).
_apply_resolve_session() {
  if [[ -n "${AGENTIC_SESSION:-}" ]]; then
    local dir=".claude/sessions/$AGENTIC_SESSION"
    [[ -d "$dir" ]] && echo "$dir" || echo ""
    return
  fi

  local newest
  newest=$(ls -td .claude/sessions/*/ 2>/dev/null | grep -v '/queued_' | head -1)
  if [[ -n "$newest" && -d "$newest" ]]; then
    echo "${newest%/}"
  else
    echo ""
  fi
}

# Get task IDs in execution order from a tasks.json file
_apply_get_task_ids() {
  local tasks_file="$1"
  local task_ids
  task_ids=($(jq -r '.execution_order[]?' "$tasks_file" 2>/dev/null))
  [[ ${#task_ids[@]} -eq 0 ]] && task_ids=($(jq -r '.tasks[]?.id' "$tasks_file"))
  echo "${task_ids[@]}"
}

# ─────────────────────────────────────────────────────────────────────────────
# Repo map — compact symbol index of the project
# Returns lines of the form: "src/hooks/useFoo.ts: FooReturn, useFoo"
# Gives the model a table of contents so it knows where every symbol lives
# without having to read every file.
# ─────────────────────────────────────────────────────────────────────────────

_build_repo_map() {
  "${AGENTIC_HOME}/venv/bin/python3" - <<'PYEOF'
import os, re

EXCLUDES = {'node_modules', '.git', 'dist', 'build', '.next', '.claude',
            'coverage', '__pycache__', '.turbo', 'out', '.vercel',
            'worktrees', 'queue'}

# Matches: export [async|default|declare]* (function*|const|let|var|class|interface|type|enum|abstract class) NAME
EXPORT_RE = re.compile(
    r'^export\s+(?:(?:async|default|declare|abstract)\s+)*'
    r'(?:function\*?\s+|const\s+|let\s+|var\s+|class\s+|interface\s+|type\s+|enum\s+)'
    r'(\w+)',
    re.MULTILINE,
)
# Matches: export { Foo, Bar as Baz }
REEXPORT_RE = re.compile(r'^export\s+\{([^}]+)\}', re.MULTILINE)

lines = []
for root, dirs, files in os.walk('.'):
    dirs[:] = sorted(d for d in dirs if d not in EXCLUDES and not d.startswith('.'))
    for fname in sorted(files):
        if not (fname.endswith('.ts') or fname.endswith('.tsx')):
            continue
        path = os.path.join(root, fname)
        rel = path[2:] if path.startswith('./') else path
        try:
            content = open(path, errors='replace').read()
        except Exception:
            continue
        names = [m.group(1) for m in EXPORT_RE.finditer(content)]
        for m in REEXPORT_RE.finditer(content):
            for part in m.group(1).split(','):
                n = part.strip().split(' as ')[0].strip()
                if n and n not in ('default', ''):
                    names.append(n)
        seen, unique = set(), []
        for n in names:
            if n not in seen:
                seen.add(n)
                unique.append(n)
        if unique:
            lines.append(f'{rel}: {", ".join(unique)}')

print('\n'.join(lines))
PYEOF
}

# ─────────────────────────────────────────────────────────────────────────────
# .llmignore support
# ─────────────────────────────────────────────────────────────────────────────

# Cached patterns array — populated once per shell session
_LLMIGNORE_PATTERNS=()
_LLMIGNORE_LOADED=false

# Load patterns from .llmignore (gitignore-style: globs, # comments, blank lines)
_llmignore_load() {
  _LLMIGNORE_PATTERNS=()
  _LLMIGNORE_LOADED=true

  local ignore_file="${1:-.llmignore}"
  [[ ! -f "$ignore_file" ]] && return

  while IFS= read -r line; do
    # Strip carriage returns, skip blanks and comments
    line="${line//$'\r'/}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    _LLMIGNORE_PATTERNS+=("$line")
  done < "$ignore_file"
}

# Test whether a single path should be ignored.
# Returns 0 (ignored) or 1 (not ignored).
_llmignore_match() {
  local path="$1"

  # Load patterns on first call
  [[ "$_LLMIGNORE_LOADED" == false ]] && _llmignore_load

  # Nothing to match against
  [[ ${#_LLMIGNORE_PATTERNS[@]} -eq 0 ]] && return 1

  # Normalise: strip leading ./
  path="${path#./}"

  local pattern
  for pattern in "${_LLMIGNORE_PATTERNS[@]}"; do
    # Strip leading ./
    pattern="${pattern#./}"

    # Directory pattern (trailing slash) — match path prefix
    if [[ "$pattern" == */ ]]; then
      local dir="${pattern%/}"
      # Match if path starts with dir/ or equals dir
      if [[ "$path" == "$dir" || "$path" == "$dir/"* ]]; then
        return 0
      fi
      continue
    fi

    # ** glob — delegate to bash case for recursive matching
    if [[ "$pattern" == *"**"* ]]; then
      # Convert **/ to a form bash case can handle: match any prefix
      local regex="${pattern//\*\*/DOUBLESTAR}"
      # We'll use a manual prefix check instead
      # Pattern like "foo/**/bar" — check if path contains foo/.../bar
      local prefix="${pattern%%/**/*}"
      local suffix="${pattern##*/**/}"
      if [[ "$prefix" != "$pattern" && "$suffix" != "$pattern" ]]; then
        # Has ** in middle
        [[ "$path" == $prefix* && "$path" == *$suffix ]] && return 0
        continue
      fi
      # Pattern like "**/foo" — match foo anywhere in path
      if [[ "$pattern" == "**/"* ]]; then
        local tail="${pattern#**/}"
        # Match basename or any path segment
        if [[ "$path" == $tail || "$path" == */$tail ]]; then
          return 0
        fi
        # Also handle directory prefix: **/foo matches foo/bar/baz
        if [[ "$path" == $tail/* ]]; then
          return 0
        fi
        continue
      fi
      # Pattern like "foo/**" — match anything under foo/
      if [[ "$pattern" == *"/**" ]]; then
        local base="${pattern%/**}"
        [[ "$path" == "$base/"* || "$path" == "$base" ]] && return 0
        continue
      fi
    fi

    # Simple filename pattern (no slash) — match against basename only
    if [[ "$pattern" != */* ]]; then
      local basename="${path##*/}"
      # shellcheck disable=SC2254
      case "$basename" in
        $pattern) return 0 ;;
      esac
      continue
    fi

    # Pattern with slash — match against full path
    # shellcheck disable=SC2254
    case "$path" in
      $pattern) return 0 ;;
    esac
  done

  return 1
}

# Filter stdin paths through .llmignore — outputs only non-ignored paths
_llmignore_filter() {
  while IFS= read -r path; do
    _llmignore_match "$path" || echo "$path"
  done
}

# ─────────────────────────────────────────────────────────────────────────────
# Interactive session switcher
# ─────────────────────────────────────────────────────────────────────────────

function agentic-use() {
  local sessions_dir=".claude/sessions"

  if [[ ! -d "$sessions_dir" ]] || [[ -z "$(ls -A "$sessions_dir" 2>/dev/null)" ]]; then
    echo "❌ No sessions found in $sessions_dir"
    return 1
  fi

  echo "📚 Available sessions:"
  echo ""

  local sessions
  sessions=($(ls -t "$sessions_dir"))
  local i=1

  for session in "${sessions[@]}"; do
    echo "  $i) $session"
    if [[ -f "$sessions_dir/$session/tasks.json" ]]; then
      local task_count
      task_count=$(jq -r '.tasks | length' "$sessions_dir/$session/tasks.json" 2>/dev/null)
      [[ -n "$task_count" && "$task_count" != "null" ]] && echo "     Tasks: $task_count"
    fi
    ((i++))
    echo ""
  done

  read -p "Choose session number: " choice

  if [[ "$choice" -gt 0 && "$choice" -le "${#sessions[@]}" ]] 2>/dev/null; then
    local selected="${sessions[$((choice - 1))]}"

    export AGENTIC_SESSION="$selected"

    echo "✅ Active session: $selected"
    echo ""
    echo "⚠️  To persist in your current shell:"
    echo "    export AGENTIC_SESSION=\"$selected\""
  else
    echo "❌ Invalid choice"
    return 1
  fi
}
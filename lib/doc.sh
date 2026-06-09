#!/bin/bash
# Documentation functions

function agentic-doc() {
  ${EDITOR:-nano} CLAUDE.md
}

function _doc_analyze_typescript() {
  local analysis_file="$1"

  echo "=== PACKAGE.JSON ===" > "$analysis_file"
  if [[ -f "package.json" ]]; then cat package.json >> "$analysis_file"
  else echo "No package.json found" >> "$analysis_file"; fi
  echo "" >> "$analysis_file"

  echo "=== PROJECT STRUCTURE ===" >> "$analysis_file"
  if command -v tree &> /dev/null && [[ ! -f ".llmignore" ]]; then
    tree -L 4 -I 'node_modules|.git|dist|build|coverage|.next' --dirsfirst . >> "$analysis_file" 2>/dev/null
  else
    find . -type f \( -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.jsx" \) \
      -not -path "*/node_modules/*" -not -path "*/.git/*" -not -path "*/dist/*" \
      -not -path "*/build/*" 2>/dev/null | sort | _llmignore_filter >> "$analysis_file"
  fi
  echo "" >> "$analysis_file"

  echo "=== TEST FRAMEWORK CONFIG ===" >> "$analysis_file"
  for cfg in vitest.config.ts vitest.config.js jest.config.ts jest.config.js jest.config.cjs vitest.workspace.ts; do
    [[ -f "$cfg" ]] && { echo "--- $cfg ---" >> "$analysis_file"; cat "$cfg" >> "$analysis_file"; echo "" >> "$analysis_file"; }
  done
  echo "" >> "$analysis_file"

  echo "=== LINTER / FORMATTER CONFIG ===" >> "$analysis_file"
  for cfg in eslint.config.ts eslint.config.js eslint.config.mjs \
    .eslintrc .eslintrc.js .eslintrc.json .eslintrc.yml \
    prettier.config.ts prettier.config.js .prettierrc .prettierrc.json .editorconfig; do
    [[ -f "$cfg" ]] && { echo "--- $cfg ---" >> "$analysis_file"; cat "$cfg" >> "$analysis_file"; echo "" >> "$analysis_file"; }
  done
  echo "" >> "$analysis_file"

  echo "=== TSCONFIG ===" >> "$analysis_file"
  [[ -f "tsconfig.json" ]] && cat tsconfig.json >> "$analysis_file"
  echo "" >> "$analysis_file"

  echo "📋 Extracting test patterns..."
  echo "=== TEST FILE ANALYSIS ===" >> "$analysis_file"
  local test_files=($(find . -type f \( -name "*.test.ts" -o -name "*.test.tsx" \
    -o -name "*.test.js" -o -name "*.test.jsx" -o -name "*.spec.ts" -o -name "*.spec.tsx" \
    \) -not -path "*/node_modules/*" -not -path "*/.git/*" 2>/dev/null \
    | _llmignore_filter | head -10))
  if [[ ${#test_files[@]} -gt 0 ]]; then
    echo "Test files found: ${#test_files[@]}" >> "$analysis_file"
    for test_file in "${test_files[@]:0:3}"; do
      echo "" >> "$analysis_file"; echo "--- $test_file ---" >> "$analysis_file"
      head -30 "$test_file" >> "$analysis_file"
    done
  else
    echo "No test files found" >> "$analysis_file"
  fi
  echo "" >> "$analysis_file"

  echo "🔍 Extracting real export patterns..."
  echo "=== EXPORT PATTERNS (real lines from source) ===" >> "$analysis_file"
  while IFS= read -r f; do
    grep -m 3 -E "^export (default |const |function |class |type |interface |enum )" "$f" 2>/dev/null \
      | sed "s|^|$f: |" || true
  done < <(find . -type f \( -name "*.ts" -o -name "*.tsx" \) \
    -not -path "*/node_modules/*" -not -path "*/.git/*" -not -name "*.test.*" -not -name "*.spec.*" \
    2>/dev/null | _llmignore_filter | head -30) >> "$analysis_file"
  echo "" >> "$analysis_file"

  echo "=== IMPORT PATTERNS (real lines from source) ===" >> "$analysis_file"
  while IFS= read -r f; do
    grep -m 3 -E "^import " "$f" 2>/dev/null | sed "s|^|$f: |" || true
  done < <(find . -type f \( -name "*.ts" -o -name "*.tsx" \) \
    -not -path "*/node_modules/*" -not -path "*/.git/*" -not -name "*.test.*" -not -name "*.spec.*" \
    2>/dev/null | _llmignore_filter | head -20) >> "$analysis_file"
  echo "" >> "$analysis_file"

  echo "=== FUNCTION PATTERNS (real signatures from source) ===" >> "$analysis_file"
  while IFS= read -r f; do
    grep -m 2 -E "^(export )?(async )?function |^export const [a-zA-Z]+ = (async )?\(" "$f" 2>/dev/null \
      | sed "s|^|$f: |" || true
  done < <(find . -type f \( -name "*.ts" -o -name "*.tsx" \) \
    -not -path "*/node_modules/*" -not -path "*/.git/*" -not -name "*.test.*" -not -name "*.spec.*" \
    2>/dev/null | _llmignore_filter | head -20) >> "$analysis_file"
  echo "" >> "$analysis_file"

  echo "📝 Collecting source samples..."
  echo "=== SOURCE FILE SAMPLES ===" >> "$analysis_file"
  local source_files=($(find . -type f \( -name "*.ts" -o -name "*.tsx" \) \
    -not -path "*/node_modules/*" -not -path "*/.git/*" -not -path "*/dist/*" \
    -not -name "*.test.*" -not -name "*.spec.*" 2>/dev/null | _llmignore_filter | head -20))
  for f in "${source_files[@]:0:3}"; do
    local lines=$(wc -l < "$f" | tr -d ' ')
    echo "" >> "$analysis_file"; echo "--- $f ($lines lines) ---" >> "$analysis_file"
    if [[ $lines -le 60 ]]; then cat "$f" >> "$analysis_file"
    else
      local mid=$(( lines / 2 ))
      echo "[First 20 lines]" >> "$analysis_file"; head -20 "$f" >> "$analysis_file"
      echo "" >> "$analysis_file"
      echo "[Lines $((mid-10))-$((mid+10)) — middle of file]" >> "$analysis_file"
      sed -n "$((mid-10)),$((mid+10))p" "$f" >> "$analysis_file"
    fi
  done
  echo "" >> "$analysis_file"
}

function _doc_analyze_gameboy() {
  local analysis_file="$1"

  echo "=== MAKEFILE ===" > "$analysis_file"
  if [[ -f "Makefile" ]]; then cat Makefile >> "$analysis_file"
  else echo "No Makefile found" >> "$analysis_file"; fi
  echo "" >> "$analysis_file"

  echo "=== PROJECT STRUCTURE ===" >> "$analysis_file"
  if command -v tree &> /dev/null; then
    tree -L 4 -I '.git|build|assets' --dirsfirst . >> "$analysis_file" 2>/dev/null
  else
    find . -type f \( -name "*.c" -o -name "*.h" \) \
      -not -path "*/.git/*" -not -path "*/build/*" \
      2>/dev/null | sort | _llmignore_filter >> "$analysis_file"
  fi
  echo "" >> "$analysis_file"

  echo "=== GBDK / TOOLCHAIN CONFIG ===" >> "$analysis_file"
  echo "GBDK_HOME=${GBDK_HOME:-not set}" >> "$analysis_file"
  [[ -f ".clang-format" ]] && { echo "--- .clang-format ---" >> "$analysis_file"; cat .clang-format >> "$analysis_file"; echo "" >> "$analysis_file"; }
  echo "" >> "$analysis_file"

  echo "📋 Extracting function signatures..."
  echo "=== FUNCTION SIGNATURES (real definitions from source) ===" >> "$analysis_file"
  while IFS= read -r f; do
    grep -m 5 -E "^[a-zA-Z_][a-zA-Z0-9_ *]+[[:space:]]+[a-zA-Z_]\w*[[:space:]]*\(" "$f" 2>/dev/null \
      | grep -v "^\s*//" | sed "s|^|$f: |" || true
  done < <(find . -type f -name "*.c" \
    -not -path "*/.git/*" -not -path "*/build/*" \
    2>/dev/null | _llmignore_filter | head -20) >> "$analysis_file"
  echo "" >> "$analysis_file"

  echo "=== HEADER DECLARATIONS ===" >> "$analysis_file"
  while IFS= read -r f; do
    grep -m 10 -E "^(void|UINT8|INT8|UINT16|INT16|UINT32|INT32)[[:space:]]" "$f" 2>/dev/null \
      | sed "s|^|$f: |" || true
  done < <(find . -type f -name "*.h" \
    -not -path "*/.git/*" -not -path "*/build/*" \
    2>/dev/null | _llmignore_filter | head -10) >> "$analysis_file"
  echo "" >> "$analysis_file"

  echo "=== INCLUDE PATTERNS ===" >> "$analysis_file"
  while IFS= read -r f; do
    grep -m 5 -E "^#include" "$f" 2>/dev/null | sed "s|^|$f: |" || true
  done < <(find . -type f \( -name "*.c" -o -name "*.h" \) \
    -not -path "*/.git/*" -not -path "*/build/*" \
    2>/dev/null | _llmignore_filter | head -20) >> "$analysis_file"
  echo "" >> "$analysis_file"

  echo "📝 Collecting source samples..."
  echo "=== SOURCE FILE SAMPLES ===" >> "$analysis_file"
  local source_files=($(find . -type f -name "*.c" \
    -not -path "*/.git/*" -not -path "*/build/*" \
    2>/dev/null | _llmignore_filter | head -10))
  for f in "${source_files[@]:0:4}"; do
    local lines=$(wc -l < "$f" | tr -d ' ')
    echo "" >> "$analysis_file"; echo "--- $f ($lines lines) ---" >> "$analysis_file"
    if [[ $lines -le 80 ]]; then cat "$f" >> "$analysis_file"
    else
      echo "[First 40 lines]" >> "$analysis_file"; head -40 "$f" >> "$analysis_file"
    fi
  done
  echo "" >> "$analysis_file"
}

function agentic-doc-gen() {
  echo "🔍 Analyzing project to generate CLAUDE.md..."

  if [[ -f "CLAUDE.md" ]]; then
    read -p "CLAUDE.md exists. Overwrite? (y/n) " overwrite
    if [[ "$overwrite" =~ ^[Yy]$ ]]; then
      mv CLAUDE.md CLAUDE.md.backup
      echo "✅ Backed up existing CLAUDE.md"
    else
      return 0
    fi
  fi

  [[ -f ".llmignore" ]] && echo "🚫 .llmignore active ($(grep -c '^[^#]' .llmignore) patterns)"

  local analysis_file="/tmp/project_analysis_$(date +%s).txt"

  # Detect language profile for this project
  local _doc_profile
  _doc_profile=$("${AGENTIC_HOME}/venv/bin/python3" -c "
import sys; sys.path.insert(0, '${AGENTIC_HOME}/lib')
from lang_profile import detect_profile
print(detect_profile('.'))
" 2>/dev/null || echo "typescript")
  echo "📊 Analyzing project structure (profile: ${_doc_profile})..."

  if [[ "$_doc_profile" == "gameboy-c" ]]; then
    _doc_analyze_gameboy "$analysis_file"
  else
    _doc_analyze_typescript "$analysis_file"
  fi
  echo "" >> "$analysis_file"

  # ============================================================
  # 11. GENERATE CLAUDE.MD
  # ============================================================
  local documenter_prompt
  if [[ -f "$AGENTIC_HOME/agents/documenter.txt" ]]; then
    documenter_prompt="$(cat $AGENTIC_HOME/agents/documenter.txt)"
  else
    documenter_prompt="You are a technical documentation expert. Generate a CLAUDE.md file for an AI coding assistant based on the project analysis provided. Every convention must include real code examples extracted from the project files shown. Start with '# Project Documentation for Claude'."
  fi

  local user_prompt="PROJECT ANALYSIS DATA:
$(cat "$analysis_file")

Generate comprehensive CLAUDE.md documentation based on the analysis above.
Every pattern and convention MUST show a real code example extracted from the project files above.
Output ONLY markdown. Start with '# Project Documentation for Claude'."

  echo "📝 Generating CLAUDE.md with AI..."

  claude_api \
    --model "$AGENTIC_MODEL" \
    --system "$documenter_prompt" \
    --user "$user_prompt" \
    --output "CLAUDE.md.new" \
    --max-tokens 4096

  if [[ $? -ne 0 ]]; then
    echo "❌ API call failed"
    [[ -f "CLAUDE.md.backup" ]] && mv CLAUDE.md.backup CLAUDE.md
    rm -f "$analysis_file"
    return 1
  fi

  if [[ -s "CLAUDE.md.new" ]] && grep -q "# Project" "CLAUDE.md.new"; then
    mv CLAUDE.md.new CLAUDE.md
    echo ""
    echo "✅ CLAUDE.md generated!"
    [[ -f "CLAUDE.md.backup" ]] && echo "   (Backup saved as CLAUDE.md.backup)"
    echo ""
    echo "Preview (first 40 lines):"
    echo "─────────────────────────────────────"
    head -40 CLAUDE.md
    echo "..."
    echo "─────────────────────────────────────"
  else
    echo "❌ Generation failed or output invalid"
    [[ -f "CLAUDE.md.backup" ]] && mv CLAUDE.md.backup CLAUDE.md
    rm -f CLAUDE.md.new
    rm -f "$analysis_file"
    return 1
  fi

  rm -f "$analysis_file"
}
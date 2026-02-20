#!/bin/bash
# Documentation functions

function agentic-doc() {
  ${EDITOR:-nano} CLAUDE.md
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

  local analysis_file="/tmp/project_analysis_$(date +%s).txt"

  echo "📊 Analyzing project structure..."

  # ============================================================
  # 1. PACKAGE.JSON
  # ============================================================
  echo "=== PACKAGE.JSON ===" > "$analysis_file"
  if [[ -f "package.json" ]]; then
    cat package.json >> "$analysis_file"
  else
    echo "No package.json found" >> "$analysis_file"
  fi
  echo "" >> "$analysis_file"

  # ============================================================
  # 2. DIRECTORY STRUCTURE
  # ============================================================
  echo "=== PROJECT STRUCTURE ===" >> "$analysis_file"
  if command -v tree &> /dev/null; then
    if tree --version 2>&1 | grep -q "2\.[0-9]"; then
      tree -J -L 4 -I 'node_modules|.git|dist|build|coverage|.next' . 2>/dev/null >> "$analysis_file" \
        || tree -L 4 -I 'node_modules|.git|dist|build|coverage|.next' --dirsfirst . >> "$analysis_file" 2>/dev/null
    else
      tree -L 4 -I 'node_modules|.git|dist|build|coverage|.next' --dirsfirst . >> "$analysis_file" 2>/dev/null
    fi
  else
    find . -type f \( -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.jsx" \) \
      -not -path "*/node_modules/*" -not -path "*/.git/*" -not -path "*/dist/*" \
      -not -path "*/build/*" -not -path "*/.next/*" \
      2>/dev/null | sort >> "$analysis_file"
  fi
  echo "" >> "$analysis_file"

  # ============================================================
  # 3. TEST FRAMEWORK CONFIG FILES
  # ============================================================
  echo "=== TEST FRAMEWORK CONFIG ===" >> "$analysis_file"
  for cfg in vitest.config.ts vitest.config.js jest.config.ts jest.config.js jest.config.cjs vitest.workspace.ts; do
    if [[ -f "$cfg" ]]; then
      echo "--- $cfg ---" >> "$analysis_file"
      cat "$cfg" >> "$analysis_file"
      echo "" >> "$analysis_file"
    fi
  done
  echo "" >> "$analysis_file"

  # ============================================================
  # 4. TSCONFIG
  # ============================================================
  echo "=== TSCONFIG ===" >> "$analysis_file"
  if [[ -f "tsconfig.json" ]]; then
    cat tsconfig.json >> "$analysis_file"
  fi
  echo "" >> "$analysis_file"

  # ============================================================
  # 5. TEST FILES — REAL EXAMPLES
  # ============================================================
  echo "📋 Extracting test patterns..."
  echo "=== TEST FILE ANALYSIS ===" >> "$analysis_file"

  local test_files=($(find . -type f \( \
    -name "*.test.ts" -o -name "*.test.tsx" \
    -o -name "*.test.js" -o -name "*.test.jsx" \
    -o -name "*.spec.ts" -o -name "*.spec.tsx" \
    \) -not -path "*/node_modules/*" -not -path "*/.git/*" 2>/dev/null | head -10))

  if [[ ${#test_files[@]} -gt 0 ]]; then
    echo "Test files found: ${#test_files[@]}" >> "$analysis_file"
    echo "" >> "$analysis_file"

    echo "Test file paths (shows location pattern):" >> "$analysis_file"
    for f in "${test_files[@]}"; do
      echo "  $f" >> "$analysis_file"
    done
    echo "" >> "$analysis_file"

    echo "Test file contents (first 30 lines each — shows real import and describe patterns):" >> "$analysis_file"
    for test_file in "${test_files[@]:0:3}"; do
      echo "" >> "$analysis_file"
      echo "--- $test_file ---" >> "$analysis_file"

      local base=$(basename "$test_file")
      base="${base%.test.ts}" base="${base%.test.tsx}" base="${base%.spec.ts}" base="${base%.spec.tsx}"
      base="${base%.test.js}" base="${base%.test.jsx}"
      local test_dir=$(dirname "$test_file")

      local source_file=""
      for ext in ts tsx js jsx; do
        if [[ -f "$test_dir/$base.$ext" ]]; then
          source_file="$test_dir/$base.$ext"
          break
        fi
      done

      if [[ -n "$source_file" ]]; then
        echo "Corresponding source: $source_file" >> "$analysis_file"
      else
        echo "Corresponding source: not found in same directory" >> "$analysis_file"
      fi

      echo "" >> "$analysis_file"
      head -30 "$test_file" >> "$analysis_file"
    done
  else
    echo "No test files found" >> "$analysis_file"
  fi
  echo "" >> "$analysis_file"

  # ============================================================
  # 6. REAL EXPORT EXAMPLES
  # ============================================================
  echo "🔍 Extracting real export patterns..."
  echo "=== EXPORT PATTERNS (real lines from source) ===" >> "$analysis_file"

 while IFS= read -r f; do
  grep -m 3 -E "^export (default |const |function |class |type |interface |enum )" "$f" 2>/dev/null \
    | sed "s|^|$f: |" || true
done < <(find . -type f \( -name "*.ts" -o -name "*.tsx" \) \
  -not -path "*/node_modules/*" -not -path "*/.git/*" \
  -not -name "*.test.*" -not -name "*.spec.*" \
  2>/dev/null | head -30) >> "$analysis_file"

  # ============================================================
  # 7. REAL IMPORT EXAMPLES
  # ============================================================
  echo "=== IMPORT PATTERNS (real lines from source) ===" >> "$analysis_file"

  while IFS= read -r f; do
  grep -m 3 -E "^import " "$f" 2>/dev/null \
    | sed "s|^|$f: |" || true
done < <(find . -type f \( -name "*.ts" -o -name "*.tsx" \) \
  -not -path "*/node_modules/*" -not -path "*/.git/*" \
  -not -name "*.test.*" -not -name "*.spec.*" \
  2>/dev/null | head -20) >> "$analysis_file"

  # ============================================================
  # 8. REAL ASYNC/FUNCTION PATTERN EXAMPLES
  # ============================================================
  echo "=== FUNCTION PATTERNS (real signatures from source) ===" >> "$analysis_file"

 while IFS= read -r f; do
  grep -m 2 -E "^(export )?(async )?function |^export const [a-zA-Z]+ = (async )?\(" "$f" 2>/dev/null \
    | sed "s|^|$f: |" || true
done < <(find . -type f \( -name "*.ts" -o -name "*.tsx" \) \
  -not -path "*/node_modules/*" -not -path "*/.git/*" \
  -not -name "*.test.*" -not -name "*.spec.*" \
  2>/dev/null | head -20) >> "$analysis_file"

  # ============================================================
  # 9. FULL SOURCE FILE SAMPLES (middle content, not just head)
  # ============================================================
  echo "📝 Collecting source samples..."
  echo "=== SOURCE FILE SAMPLES ===" >> "$analysis_file"

  local source_files=($(find . -type f \( -name "*.ts" -o -name "*.tsx" \) \
    -not -path "*/node_modules/*" -not -path "*/.git/*" -not -path "*/dist/*" \
    -not -name "*.test.*" -not -name "*.spec.*" 2>/dev/null | head -20))

  for f in "${source_files[@]:0:3}"; do
    local lines=$(wc -l < "$f" | tr -d ' ')
    echo "" >> "$analysis_file"
    echo "--- $f ($lines lines) ---" >> "$analysis_file"

    if [[ $lines -le 60 ]]; then
      cat "$f" >> "$analysis_file"
    else
      local mid=$(( lines / 2 ))
      echo "[First 20 lines]" >> "$analysis_file"
      head -20 "$f" >> "$analysis_file"
      echo "" >> "$analysis_file"
      echo "[Lines $((mid-10))-$((mid+10)) — middle of file]" >> "$analysis_file"
      sed -n "$((mid-10)),$((mid+10))p" "$f" >> "$analysis_file"
    fi
  done
  echo "" >> "$analysis_file"

  # ============================================================
  # 10. GENERATE CLAUDE.MD
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
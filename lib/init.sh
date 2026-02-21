#!/bin/bash
# Project initialization

function agentic-init() {
  echo "🎯 Initialize Agentic Workflow"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  
  if [[ -d ".claude" ]] || [[ -f "CLAUDE.md" ]]; then
    echo "⚠️  Project appears to be already initialized:"
    [[ -d ".claude" ]] && echo "  ✓ .claude/ directory exists"
    [[ -f "CLAUDE.md" ]] && echo "  ✓ CLAUDE.md exists"
    echo ""
    read -p "Reinitialize anyway? (y/n) " reinit
    if [[ ! "$reinit" =~ ^[Yy]$ ]]; then
      echo "Cancelled"
      return 0
    fi
  fi
  
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "Setting up project structure..."
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  
  # ── .claude/ directories ───────────────────────────────────────────────────
  mkdir -p .claude/{sessions,plans,metrics}
  echo "✅ Created .claude/ directory"
  
  # ── .gitignore ─────────────────────────────────────────────────────────────
  if [[ -f ".gitignore" ]]; then
    if ! grep -q ".claude/" .gitignore; then
      echo "" >> .gitignore
      echo "# Agentic Workflow" >> .gitignore
      echo ".claude/" >> .gitignore
      echo "*.backup" >> .gitignore
      echo "✅ Updated .gitignore"
    else
      echo "✅ .gitignore already configured"
    fi
  else
    cat > .gitignore <<GITIGNORE
# Agentic Workflow
.claude/
*.backup
GITIGNORE
    echo "✅ Created .gitignore"
  fi

  # ── .vscode/settings.json ──────────────────────────────────────────────────
  mkdir -p .vscode
  if [[ -f ".vscode/settings.json" ]]; then
    local tmp
    tmp=$(mktemp)
    # Strip // line comments so jq can parse JSONC, then merge
    if sed 's|[[:space:]]*//.*||g' .vscode/settings.json \
        | jq '. * {
          "files.associations": {".llmignore": "ignore"},
          "material-icon-theme.files.associations": {".llmignore": ".gitignore"}
        }' > "$tmp" 2>/dev/null && [[ -s "$tmp" ]]; then
      mv "$tmp" .vscode/settings.json
      echo "✅ Updated .vscode/settings.json"
    else
      rm -f "$tmp"
      # Fallback: write keys before closing brace using temp file (cross-platform)
      cp .vscode/settings.json .vscode/settings.json.bak
      local tmp2
      tmp2=$(mktemp)
      # Remove last closing brace, append new keys, re-close
      head -n -1 .vscode/settings.json > "$tmp2"
      cat >> "$tmp2" <<'JSONKEYS'
  ,
  "files.associations": {".llmignore": "ignore"},
  "material-icon-theme.files.associations": {".llmignore": ".gitignore"}
}
JSONKEYS
      mv "$tmp2" .vscode/settings.json
      echo "✅ Updated .vscode/settings.json (backup at .vscode/settings.json.bak)"
    fi
  else
    cat > .vscode/settings.json <<'VSCODE'
{
  "files.associations": {
    ".llmignore": "ignore"
  },
  "material-icon-theme.files.associations": {
    ".llmignore": ".gitignore"
  }
}
VSCODE
    echo "✅ Created .vscode/settings.json"
  fi

  # ── .llmignore ─────────────────────────────────────────────────────────────
  if [[ ! -f ".llmignore" ]]; then
    if [[ -f "$AGENTIC_HOME/.llmignore.example" ]]; then
      cp "$AGENTIC_HOME/.llmignore.example" .llmignore
      echo "✅ Created .llmignore from example"
      echo "   Edit to exclude files from AI context: nano .llmignore"
    fi
  else
    echo "✅ .llmignore already exists"
  fi

  # ── CLAUDE.md ──────────────────────────────────────────────────────────────
  echo ""
  if [[ -f "CLAUDE.md" ]]; then
    read -p "CLAUDE.md exists. Regenerate? (y/n) " regen
    if [[ "$regen" =~ ^[Yy]$ ]]; then
      agentic-doc-gen
    fi
  else
    echo "CLAUDE.md not found."
    echo ""
    echo "Options:"
    echo "  1. Generate automatically (analyzes project)"
    echo "  2. Create template manually"
    echo "  3. Skip for now"
    echo ""
    read -p "Choice (1/2/3): " doc_choice
    
    case "$doc_choice" in
      1)
        agentic-doc-gen
        ;;
      2)
        cat > CLAUDE.md <<'TEMPLATE'
# Project Documentation for Claude

## Overview
[Describe what this project does - purpose, main features, target users]

## Tech Stack
- **Language**: [TypeScript/JavaScript/Python/etc]
- **Framework**: [React/Next.js/Express/etc]
- **Build Tool**: [Vite/Webpack/etc]
- **Package Manager**: [npm/yarn/pnpm]

## Testing Conventions
- **Framework**: [Vitest/Jest/Mocha/Pytest]
- **Test File Pattern**: [*.test.ts / *.spec.ts / test_*.py]
- **Test Location**: 
  - [ ] Co-located (tests are in same directory as source files)
  - [ ] Separate directory (tests mirror source structure in test/ directory)

**Examples of test location pattern**:
- Source file: \`________\` → Test file: \`________\`
- Source file: \`________\` → Test file: \`________\`

**IMPORTANT for AI**: When creating test tasks, ALWAYS specify the full path to the source file being tested in the task description.
Example: "Create tests for app/utils/validation.ts" NOT "test the validation utilities"

## Code Standards & Conventions
- [Naming conventions - camelCase, PascalCase, etc]
- [File organization patterns]
- [Import/export preferences]
- [Error handling patterns]
- [Async patterns - async/await vs promises]

## Project-Specific Notes
- [Any critical information about the codebase]
- [Common pitfalls to avoid]
- [Performance considerations]
- [Security requirements]

## Dependencies - Available Packages
[This will be auto-populated from package.json]
Only use packages listed above - do not assume packages are available.

## Examples
Provide 1-2 examples of typical files in this project to show the style:

\`\`\`typescript
// Example component/function/module
[paste a representative example]
\`\`\`

TEMPLATE
        echo "✅ Created CLAUDE.md template"
        echo "   Edit with: agentic doc"
        ;;
      3)
        echo "⏭️  Skipped CLAUDE.md"
        ;;
    esac
  fi
  
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "✅ Initialization Complete!"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  echo "Project initialized with:"
  echo "  ✓ .claude/ directory for sessions"
  echo "  ✓ .gitignore configured"
  echo "  ✓ .vscode/settings.json configured"
  [[ -f ".llmignore" ]] && echo "  ✓ .llmignore (edit to exclude files from AI context)"
  [[ -f "CLAUDE.md" ]] && echo "  ✓ CLAUDE.md project documentation"
  echo ""
  echo "Next steps:"
  echo "  1. Edit .llmignore to exclude sensitive/noisy files"
  echo "  2. Edit CLAUDE.md to document your project conventions"
  echo "  3. Review with: agentic doc"
  echo "  4. Start workflow: agentic"
  echo "  5. Or plan feature: agentic plan"
  echo ""
  
  read -p "Start workflow now? (y/n) " start_now
  if [[ "$start_now" =~ ^[Yy]$ ]]; then
    echo ""
    agentic
  fi
}
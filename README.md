# Agentic Workflow

An AI-powered development workflow that turns natural language requests into production-ready code through intelligent planning, context-aware generation, multi-level validation, and safe application.

## 🎯 What It Does

Agentic discovers your project structure, learns your patterns, generates code with full context awareness, validates quality at multiple levels, and safely applies changes—all while maintaining git safety and providing detailed metrics.

**Workflow:**
```
Your Request → Plan (Architect) → Generate (Implementor) → Validate → Apply → Done
                     ↓                        ↓                ↓ (if issues)
              Discovers structure    Reads actual source    Refine → Iterate
```

---

## 🚀 Installation

### Requirements

- **zsh**
- **jq**: `brew install jq`
- **curl** (standard on macOS)
- **git** (recommended for safety features)
- **Ollama** (for local models) or an Anthropic API key

### Quick Install
```bash
# 1. Clone to ~/.agentic
git clone <repo-url> ~/.agentic

# 2. Run installer
bash ~/.agentic/install.sh

# 3. Edit config with your settings
nano ~/.agentic/.agentic.conf

# 4. Reload shell
source ~/.zshrc

# 5. Verify
agentic --help
```

The installer will:
- Make the `agentic` bin executable
- Copy `.agentic.conf.example` → `.agentic.conf` (if not already present)
- Add `AGENTIC_HOME`, `PATH`, and config sourcing to `.zshrc`

---

## ⚙️ Configuration

`~/.agentic/.agentic.conf` holds your settings and secrets. It is **gitignored** — never committed to the repo. The repo ships `.agentic.conf.example` as a safe template.

### First-Time Setup
```bash
agentic config
```

This detects whether you're using the Anthropic API or a local Ollama endpoint and prompts accordingly.

### Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIC_MODEL` | `qwen2.5-coder:32b` | Model name |
| `ANTHROPIC_BASE_URL` | `http://localhost:11434` | API endpoint (Ollama or Anthropic) |
| `ANTHROPIC_AUTH_TOKEN` | `ollama` | Auth token for Ollama/local endpoints |
| `ANTHROPIC_API_KEY` | _(empty)_ | API key for Anthropic's API |
| `OLLAMA_MAX_LOADED_MODELS` | `3` | Max concurrent models |
| `OLLAMA_KEEP_ALIVE` | `30m` | Model cache duration |

**For Anthropic API:** set `ANTHROPIC_BASE_URL=https://api.anthropic.com` and `ANTHROPIC_API_KEY=sk-ant-...`

**For Ollama:** set `ANTHROPIC_BASE_URL=http://localhost:11434` and `ANTHROPIC_AUTH_TOKEN=ollama`

`agentic config` handles this automatically — it detects the backend from the URL and saves the correct variables.

### Agent Prompts

System prompts are in `~/.agentic/agents/`:
- `architect.txt` — Task breakdown logic (required)
- `implementor.txt` — Code generation rules (required)
- `documenter.txt` — Documentation generation (optional)
- `planner.txt` — Agile story breakdown (optional)

---

## 📖 Usage

### Complete Workflow (Recommended)
```bash
cd my-project

# 1. Initialize (first time only)
agentic init

# 2. Run the full workflow
agentic
# Enter: "Add user authentication to the login form"
# → Discovers structure
# → Plans tasks
# → Generates code
# → Validates quality
# → Applies safely
```

The orchestrator runs up to 5 iterations of implement → validate → refine before asking whether to proceed or stop.

### Step-by-Step (Manual Control)
```bash
agentic archie          # Plan — creates .claude/latest/tasks.json
agentic implement       # Generate code → .claude/latest/outputs/
agentic validate        # Check quality
agentic apply --dry-run # Preview changes
agentic apply           # Apply for real
```

### Iterative Refinement
```bash
# If validation fails:
agentic refine          # Reads issues, improves plan, re-runs architect
agentic implement       # Regenerate with improved plan
agentic apply
```

---

## 🎮 Commands

### Main Commands

| Command | Description |
|---------|-------------|
| `agentic` | Run complete workflow with orchestration |
| `agentic init` | Initialize project (creates `.claude/`, `CLAUDE.md`) |
| `agentic config` | Configure settings (model, endpoint, API key) |
| `agentic plan` | Create agile plan with user stories and acceptance criteria |
| `agentic list` | List past sessions |
| `agentic use` | Switch to a previous session interactively |
| `agentic retry` | Regenerate `tasks.json` for a corrupted session |
| `agentic metrics [file]` | View session performance metrics |
| `agentic doc` | Open `CLAUDE.md` in `$EDITOR` |
| `agentic doc-gen` | Auto-generate `CLAUDE.md` from project analysis |

### Low-Level Commands (Expert Mode)

| Command | Description |
|---------|-------------|
| `agentic archie` | Create task breakdown only |
| `agentic implement` | Generate code for all tasks |
| `agentic validate` | Run quality checks |
| `agentic apply` | Apply changes to files |
| `agentic apply --dry-run` | Preview changes without applying |
| `agentic verify` | Verify applied changes match plan |
| `agentic refine` | Refine plan based on validation issues |

---

## 📂 Project Structure

### What Gets Created in Your Project
```
your-project/
├── .claude/                    # Workflow data (gitignored)
│   ├── sessions/
│   │   └── 20260219-143000_add-auth/
│   │       ├── request.txt         # Original request
│   │       ├── context.txt         # Project context snapshot
│   │       ├── tasks.json          # Task breakdown
│   │       ├── architect_usage.json
│   │       └── outputs/
│   │           ├── task_001.txt
│   │           ├── task_001_usage.json
│   │           └── task_002.txt
│   ├── plans/                  # Agile planning outputs
│   └── metrics/                # Session performance metrics
├── CLAUDE.md                   # Project documentation for AI
└── .gitignore                  # Updated to ignore .claude/
```

### Agentic Installation Structure
```
~/.agentic/
├── bin/
│   └── agentic                 # Main CLI entry point
├── lib/
│   ├── config.sh               # Configuration management
│   ├── utils.sh                # Shared utilities (_session_slug, format_duration, etc.)
│   ├── claude-api.sh           # Direct Anthropic API client (curl-based, with retry)
│   ├── architect.sh            # Task planning & project discovery
│   ├── implement.sh            # Code generation with stitching
│   ├── validate.sh             # Multi-level validation
│   ├── apply.sh                # Safe file operations & verify-apply
│   ├── refine.sh               # Iterative improvement
│   ├── plan.sh                 # Agile planning tool
│   ├── doc.sh                  # Smart documentation generation
│   ├── metrics.sh              # Performance tracking
│   ├── retry.sh                # Session retry/regeneration
│   ├── init.sh                 # Project initialization
│   └── core.sh                 # Main orchestrator
├── agents/
│   ├── architect.txt
│   ├── implementor.txt
│   ├── documenter.txt          # optional
│   └── planner.txt             # optional
├── .agentic.conf               # Your config — gitignored
└── .agentic.conf.example       # Safe template — committed to repo
```

---

## 🔬 How It Works

### 1. Project Discovery (Architect)

The architect builds context by scanning the actual filesystem — no hardcoded assumptions about `src/` vs `app/` vs `lib/`:

1. Reads `CLAUDE.md` for project conventions
2. Lists all source files excluding `node_modules`, `dist`, `build`, etc.
3. Lists existing test files to infer co-location patterns
4. Sends to the architect agent which outputs a `tasks.json` with accurate file paths and `modification_type` per task

Each task specifies not just a file and action but a `modification_type`:

| modification_type | What it means |
|---|---|
| `full_file` | Replace or create entire file |
| `add_import` | Insert one import line |
| `add_function` | Append a new function |
| `add_type` | Insert a type/interface after imports |
| `modify_function` | Replace a specific function by name |
| `add_to_function` | Add code inside an existing function |
| `add_route` | Insert a route before the closing router tag |
| `delete_code` | Remove a specific function/export |

### 2. Context-Aware Generation (Implementor)

For each task the implementor:

- Loads only the relevant section of the source file (not the whole file) based on `modification_type` — saves tokens and improves accuracy
- For `modify_function` / `add_to_function`: uses brace-counting to locate the exact function range
- For test files, runs a 4-strategy source file discovery process:
  - **Strategy 0**: Parse full path from task description (e.g. `"Create tests for app/utils/table.ts"`)
  - **Strategy 1**: Extract `src/` path from description
  - **Strategy 2**: Derive from test filename — tries 15+ path transform patterns
  - **Strategy 3**: Project-wide `find` fallback
- Injects the actual source code into the prompt so generated tests use real function signatures

After generation, partial outputs are **stitched** back into the original file:
- `add_import` → inserted after the last existing import line
- `add_function` / `add_export` → appended to end of file
- `add_type` → inserted after imports
- `modify_function` → replaces the located line range
- `add_route` → inserted before `</Routes>` / `</Switch>`

### 3. Multi-Level Validation

**Pre-apply (inside `apply`):**
- File paths have extensions
- Output files exist for non-DELETE tasks

**Content validation (`validate`):**
- Stray markdown fences
- Placeholder/TODO markers (`...`, `// implement`, etc.)
- Brace balance for TypeScript/JS
- Vitest/Jest mixing detection in test files
- JSON validity for `.json` files
- Package import existence in `package.json`
- Relative import path resolution (checks disk + other tasks in session)
- Cross-task symbol consistency (imported names must be exported by the dependency task)
- Size regression warning (output >30 lines shorter than original)
- `modification_type` shape checks (`add_import` should be ~1 line, etc.)

Issues are saved to `validation_issues.txt` for `refine` to consume. Warnings are saved separately and don't block `apply`.

**Post-apply (`verify-apply`):**
- Confirms CREATE/MODIFY/DELETE actually happened
- For MODIFY, checks the file changed vs its backup
- For `delete_code`, checks the file still exists but differs from backup

### 4. Safe Application

`apply` runs these steps in order:
1. Pre-apply validation (file paths, output existence)
2. Git uncommitted-change warning
3. Creates a new branch `agentic/{session-id}`
4. Backs up modified files as `.backup`
5. Applies CREATE / MODIFY / DELETE operations
6. Prompts for a git commit
7. Runs `verify-apply` automatically

`apply --dry-run` shows exactly what would happen without touching the filesystem.

### 5. Iterative Refinement

`refine` reads `validation_issues.txt`, appends the issues to the original request, and re-runs the architect agent. It backs up `tasks.json` as `tasks.json.iteration-N` and clears old outputs so `implement` starts clean.

The `agentic` orchestrator loops this automatically up to 5 times before prompting the user.

---

## 📊 Smart Documentation Generation (`doc-gen`)

`agentic doc-gen` analyzes the project and generates `CLAUDE.md` using the AI. It collects:

1. `package.json` (dependencies)
2. Directory tree (via `tree` or `find`)
3. Test framework config files (vitest, jest)
4. `tsconfig.json`
5. Real test file examples (first 30 lines of up to 3 test files)
6. Real export patterns from source files
7. Real import patterns from source files
8. Real function signatures from source files
9. Full content of up to 3 representative source files

**Note on macOS compatibility:** Export/import/function pattern extraction uses `grep -m N -E` (separate flags) and process substitution `< <(find ...)` to avoid pipeline hangs under `set -euo pipefail`.

---

## 🎯 Best Practices

### Specify File Paths in Requests
```bash
✅  "Create tests for app/utils/validation.ts"
✅  "Add error handling to src/api/client.ts"
❌  "Add tests for the validation utilities"
❌  "Fix the API client"
```

The architect uses these paths in `task.description`, which is the highest-priority source for the implementor's source file discovery.

### Always Preview Before Applying
```bash
agentic apply --dry-run   # See what would change
agentic apply             # Apply when confident
```

### Keep CLAUDE.md Updated
```bash
agentic doc-gen   # Regenerate after major structural changes
agentic doc       # Open in $EDITOR for manual edits
```

### Recover a Corrupted Session
```bash
agentic use       # Pick the broken session
agentic retry     # Backs up bad tasks.json, regenerates it
agentic implement # Continue from there
```

---

## 🐛 Troubleshooting

### "No active session"
```bash
agentic list      # Show available sessions
agentic use       # Pick one interactively
# or
export AGENTIC_SESSION="20260219-143000_add-auth"
```

### "Pre-apply validation failed: no extension"
The architect produced a bad file path (e.g. `utils` instead of `utils/validation.ts`).
```bash
agentic refine    # Auto-fix via re-planning
agentic implement
agentic apply
# or manually:
nano .claude/latest/tasks.json
```

### "Test generated placeholder functions"
The implementor couldn't find the source file. Be explicit:
```bash
# Include the full path in your request:
agentic archie
> "Create tests for app/utils/table.ts"
```
Or update `CLAUDE.md` with the correct test location pattern.

### Validation keeps failing
```bash
cat .claude/latest/validation_issues.txt   # See exact issues
nano .claude/latest/outputs/task_001.txt   # Fix manually
agentic validate
agentic apply
```

### API not responding / hanging
Check `~/.agentic/.agentic.conf` — ensure `ANTHROPIC_BASE_URL` and credentials are correct. For Ollama, confirm the model is loaded: `ollama list`.

---

## 📈 Metrics

```bash
agentic metrics              # Last session
agentic metrics .claude/metrics/20260219-143000.json   # Specific file
```

**Example output:**
```
📊 Metrics Report
─────────────────────────────────────

Session:  20260219-143000_add-authentication
Status:   success
Duration: 3m 45s
Tokens:   15200

Steps:
  architect:     5s | 2100 tokens  | success
  implement_1: 125s | 11800 tokens | success
  validate_1:    2s | 0 tokens     | success
  apply:         8s | 0 tokens     | success
```

Real token counts (including cache reads and cache writes) are tracked per-task in `outputs/task_N_usage.json` and summed in the metrics report.

---

## 🚦 Quick Reference

```bash
# Setup
agentic init              # Initialize project
agentic config            # Configure model / endpoint / keys

# Main workflow
agentic                   # Complete orchestrated workflow
agentic archie            # Plan only
agentic implement         # Generate only
agentic validate          # Validate only
agentic apply --dry-run   # Preview
agentic apply             # Apply

# Iteration
agentic refine            # Fix issues and re-plan
agentic use               # Switch sessions
agentic retry             # Regenerate tasks.json

# Utilities
agentic list              # List sessions
agentic metrics           # View metrics
agentic doc               # Edit CLAUDE.md
agentic doc-gen           # Generate CLAUDE.md
agentic plan              # Agile planning
```

---

## 🤝 Contributing

- **Add validation rules**: `lib/validate.sh`
- **Improve test discovery**: `lib/implement.sh` — `_find_source_for_test()`
- **Improve stitching**: `lib/implement.sh` — `_stitch_*` helpers
- **Better prompts**: `agents/*.txt`
- **New commands**: `bin/agentic`

---

## 📝 License

MIT — use freely, modify as needed.

---

## 🙏 Philosophy

**AI should augment, not replace.**

This tool handles tedious boilerplate, file structure, imports, test scaffolding, and repetitive patterns. You focus on architecture decisions, creative solutions, code review, and business logic.

**Result:** 10x productivity without sacrificing quality.
# Agentic

Queue jobs for Claude Code (or a local Ollama model) to work on while you do something else. Review the diffs, merge what you like.

---

## What it does

You submit a request, an agent runs in an isolated git branch, does the work, verifies the build passes, and commits. You come back whenever, look at what it did, and decide whether to merge.

While jobs run, your working tree is untouched — the agent works in a separate `~/.agentic/worktrees/<id>/` checkout. Accepting a job merges the branch into your repo.

---

## Requirements

- `jq` — `brew install jq`
- `pyenv` — [github.com/pyenv/pyenv](https://github.com/pyenv/pyenv#installation) — `install.sh` uses it to pin Python 3.11.9 for the project venv
- **Cloud mode:** Claude Code CLI — [claude.ai/code](https://claude.ai/code) (authenticate on first run)
- **Local mode:** Ollama — [ollama.com](https://ollama.com) + a model (see below)

---

## Setup

```bash
git clone <repo-url> ~/.agentic
bash ~/.agentic/install.sh
source ~/.zshrc
```

---

## Usage

```bash
cd your-project
agentic serve          # cloud mode (Claude Code)
agentic serve --local  # local mode (Ollama)
```

Open [http://localhost:4080](http://localhost:4080).

**Submit a job** — describe what you want and hit Submit (`Cmd+Enter`).

**Run it** — **▶ Run Worker** for one job, **▶▶ Run All** for the queue.

**Review** — click a job to see what the agent did: files read, files modified, commands run, build result, GitHub-style split diff, chain position, phase summary, and estimated cost. Stats and diffs are preserved after you accept or reject the job.

**Merge** — **Accept Chain** collects all chained jobs onto a staging branch (`agent-work/<date>`). You merge that branch into your own work when ready.

```bash
git merge agent-work/20260520-a1b2
```

---

## Cloud vs local

| | Cloud | Local |
|---|---|---|
| Engine | Claude Code CLI | Ollama |
| Quality | Higher | Good for scoped tasks |
| Cost | Anthropic API credits | Free |
| Speed | Fast | Moderate (first job loads model) |
| Privacy | Code sent to Anthropic | Stays on your machine |

Both modes use the same queue, dashboard, worktrees, and review workflow.

---

## Local mode setup

### 1. Install Ollama

```bash
brew install ollama   # or download from ollama.com
```

### 2. Pull a model

Use the standard (non-MLX) variants for reliable tool calling performance:

```bash
# Recommended for Apple Silicon 48GB+
ollama pull qwen3.6:27b

# 16–32GB
ollama pull qwen3.6:14b   # or qwen2.5-coder:14b
```

> **Avoid MLX variants** (`-mxfp8`, `-nvfp4`, `-bf16` tags) — they run through a different Ollama backend that is significantly slower for agentic use cases involving many sequential API calls.

### 3. Create a tuned model

Create a Modelfile to set a reasonable context window and temperature:

```bash
cat > ~/Modelfile-coding <<'EOF'
FROM qwen3.6:27b
PARAMETER num_ctx 131072
PARAMETER temperature 0.2
EOF

ollama create qwen-coder -f ~/Modelfile-coding
```

### 4. Configure

Set your model in `~/.agentic/.agentic.conf`:

```bash
export AGENTIC_LOCAL_MODEL="qwen-coder"
# AGENTIC_CONTEXT_BUDGET=24000   # default — raise if your model has a larger context window
```

### 5. Start

Ollama and the agentic server are separate processes — both need to be running:

```bash
ollama serve &
agentic serve --local
```

**Specify a model on the fly:**

```bash
agentic serve --local=qwen-coder
```

---

## Model recommendations

| RAM | Model | Notes |
|---|---|---|
| 16GB | `qwen3.6:14b` or `qwen2.5-coder:14b` | Use non-MLX variant |
| 32GB | `qwen3.6:27b` | Build qwen-coder Modelfile at 64K ctx |
| 48GB+ | `qwen3.6:27b` | Build qwen-coder Modelfile at 128K ctx |

Browse [ollama.com/library](https://ollama.com/library) — filter by **tools** tag (required for function calling). Download count is the best reliability signal.

---

## Server commands

```bash
agentic serve              # cloud, port 4080
agentic serve --local      # local Ollama
agentic serve 8080         # custom port
agentic serve stop
agentic serve status
```

---

## Review workflow

| Action | What it does |
|---|---|
| **Review in IDE** | Apply agent changes as staged edits — review in VS Code Source Control, edit in place, then commit or discard (`git restore --staged . && git checkout -- .`) |
| **Accept** | Merge single job onto base branch |
| **Accept Chain ↓** | Merge whole chain onto one staging branch |
| **Reject** | Delete branch and worktree |
| **Abandon** | Move stuck running job to failed |

After **Accept Chain**, you get `agent-work/<date>`. Merge it into your branch when satisfied.

---

## Chaining jobs

Jobs can build on each other. Fill in the **Chain after** field with a previous job's ID. Each chained job branches from the previous one's committed work, so the agent sees what was actually built.

When a chained job starts, any commits that landed on the main branch after the parent job branched are automatically merged in. This means assets, CLAUDE.md updates, or other changes committed directly to main are always visible to downstream jobs — no manual sync needed.

Run the full chain with **▶▶ Run All** — it executes pending jobs in dependency order.

---

## Configuration

`~/.agentic/.agentic.conf` (gitignored):

```bash
# Local model
export AGENTIC_LOCAL_MODEL="qwen-coder"
# AGENTIC_CONTEXT_BUDGET=24000   # default — raise only if your model has a larger context window

# Ollama host (default: http://localhost:11434)
export OLLAMA_HOST="http://localhost:11434"
export OLLAMA_KEEP_ALIVE="30m"

# Optional: only needed for `agentic plan` and `agentic doc-gen`
export ANTHROPIC_API_KEY=""
export AGENTIC_MODEL="claude-opus-4-7"
```

---

## What the agent does

For each job:

1. Reads `CLAUDE.md` to understand project conventions
2. Detects the project language profile (TypeScript, Game Boy C, …) and builds a symbol map accordingly — `.ts`/`.tsx` exports for TypeScript, C function signatures for Game Boy C
3. Explores relevant source files
4. Implements the requested change
5. Calls `Setup()` — installs dependencies, detects yarn/pnpm/npm automatically
6. Calls `Build()` — reads `package.json` scripts to find the right build command, runs it
7. Runs lint if available
8. Runs Prettier if configured
9. Commits with a descriptive message

**Local mode adds a surgical repair loop on build failure:**
- One error at a time, not all at once
- Edit-only (no broad rewrites in repair mode)
- Locked to files that have errors
- Reverts automatically if repair makes things worse
- 5 escalating strategies before giving up

**Local mode safeguards:**
- `Setup()` and `Build()` tools — purpose-built for install and build steps, eliminating shell spirals
- Thinking mode disabled (`think: false`) — prevents models from spending minutes on internal reasoning per turn
- Spiral detection — 5 consecutive shell commands without a file read or edit triggers a hard stop and forces the model back to reading files
- Destructive command blocking — `rm -rf`, `killall`, `pkill` are blocked outright
- Context compression — old file reads are replaced with symbol maps when approaching the context limit

---

## Job detail page

Click any job to see what the agent did:

- **Stats** — files changed, tool calls, build ✓/✗, lint ✓/✗, token count, estimated API cost
- **Phase summary** — reads / edits / build result / commit in one bar
- **Chain visualizer** — parent → current → children, clickable
- **Files Modified** — per-file expandable GitHub-style split diff (left: removed, right: added)
- **Files Read** — every file explored
- **Commands Run** — each shell command with pass/fail, error output on failure
- **Agent Reasoning** — the full text of what the agent said (collapsible)
- **State History** — timeline of state transitions with timestamps

The detail panel auto-refreshes every 5 seconds while a job is running. Stats, diffs, and activity logs are cached and remain visible after you accept or reject a job.

---

## Dashboard features

- **Search** — filter jobs by request text, repo, or job ID
- **Console** — collapsible and resizable, height saved across sessions
- **Chain editor** — set or change a job's parent from the `⋯` menu
- **Status overrides** — manually move any job to any state from the `⋯` menu

---

## What gets created

```
~/.agentic/
├── queue/
│   ├── pending/      # waiting to run
│   ├── running/      # in progress
│   ├── done/         # ready to review
│   ├── failed/       # build error or agent failure
│   ├── abandoned/    # manually stopped
│   └── cancelled/
├── worktrees/
│   └── j_xxx/        # isolated checkout per job
├── diffs/
│   └── j_xxx.diff    # cached diff — persists after accept/reject
├── logs/
│   └── j_xxx.jsonl   # agent activity log — persists after accept/reject
└── serve.pid
```

In your project: branches named `agentic/<job-id>`. Accept Chain creates `agent-work/<date>`.

---

## Language profiles

Agentic auto-detects your project's language by inspecting files (Makefile contents, `package.json`, etc.) and selects a profile that controls how the agent explores code, parses build errors, and which tools are available.

| Profile | Detected when | Agent tools |
|---|---|---|
| `typescript` | `package.json` present | `Setup`, `Build` |
| `gameboy-c` | Makefile contains `GBDK` or `sdcc` | `Build`, `TileConvert`, `RomUsage`, `Symbols` |

**Game Boy–specific tools (local mode only):**
- `TileConvert(path, name)` — runs `png2asset` to convert a PNG to GBDK tile arrays; auto-pads images to 8px boundaries
- `RomUsage()` — parses the `.map` file and reports Bank 0 / WRAM / HRAM usage with overflow warnings
- `Symbols(filter)` — reads the `.sym` file and lists linked symbols by bank, useful for debugging linker errors

Profile detection is automatic — no configuration needed. The detected profile is stored in the job and shown in the dashboard.

---

## CLAUDE.md

The agent reads `CLAUDE.md` at project root before doing anything. Keep it current — it's the source of truth for naming conventions, import patterns, testing framework, tile index maps, and component patterns.

```bash
agentic doc-gen   # generate from project analysis (profile-aware: TypeScript or Game Boy C)
agentic doc       # open in $EDITOR
```

---

## Other commands

```bash
agentic init          # initialize project
agentic accept <id>   # merge a single job's branch
agentic reject <id>   # discard a job's branch
agentic plan          # create an agile plan
```

---

## First use walkthrough

After running `install.sh` and sourcing your shell, do this once before running any jobs.

### 1. Configure

Copy the example config and fill in what you need:

```bash
cp ~/.agentic/.agentic.conf.example ~/.agentic/.agentic.conf
```

Open it and edit:

```bash
# Cloud mode — needed for agentic plan, doc-gen, and cloud worker
export ANTHROPIC_API_KEY="sk-ant-..."

# Local mode — set the model you pulled with ollama pull
# export AGENTIC_LOCAL_MODEL="qwen-coder"
```

Everything else in the file has working defaults. `AGENTIC_CONTEXT_BUDGET` only matters in local mode.

### 2. Go to your project

All jobs run against a specific repo. Navigate there before starting the server — the dashboard picks up `$PWD` as the default repo for new jobs.

```bash
cd ~/your-project
```

### 3. Generate a CLAUDE.md

The agent reads `CLAUDE.md` at the project root before doing anything. Without it the agent guesses at conventions; with it the output is significantly better.

```bash
agentic doc-gen
```

This calls the Anthropic API to analyze your project and write a `CLAUDE.md`. Review it before running jobs — correct anything wrong about your stack, naming conventions, or test setup. Commit it to the repo so every agent run picks it up.

If you don't have an `ANTHROPIC_API_KEY` yet, skip this step and create a minimal `CLAUDE.md` by hand:

```bash
agentic doc   # opens $EDITOR with an empty or existing CLAUDE.md
```

### 4. Start the server and run your first job

```bash
agentic serve          # cloud (Claude Code)
agentic serve --local  # local (Ollama)
```

Open [http://localhost:4080](http://localhost:4080), type a request, and click **▶ Run Worker**.

---

MIT License

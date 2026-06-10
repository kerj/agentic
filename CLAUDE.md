# CLAUDE.md

Guidance for AI agents and engineers working in **agentic** — an async dev-task queue. You submit a request, an agent runs it in an isolated git worktree, and you review the diff and merge what you like. Originally a CLI toolset; the primary interface is now a browser dashboard.

> This file documents **what the code actually does today** (verified against source, June 2026), not aspirational behavior. Where the README and the code disagree, the code wins — those cases are flagged with ⚠️.

---

## What this is

- A bash CLI (`bin/agentic`) that dispatches to Python (`lib/*.py`) and bash libraries (`lib/*.sh`).
- **No `package.json`** — this is not a Node project. It's bash + Python 3.11, ~9,400 lines.
- Version: `agentic v2.0.0`, engine "Claude Code agent" ([bin/agentic](bin/agentic)).
- Two execution backends:
  - **Cloud mode** (default): shells out to the `claude` CLI (Claude Code).
  - **Local mode** (`--local`): drives a local **Ollama** model via its OpenAI-compatible API.

`AGENTIC_HOME` defaults to `~/.agentic` (the repo is installed there) and holds `bin/ lib/ agents/ profiles/ queue/ worktrees/ diffs/ logs/`.

---

## Repo layout

| Path | What it is |
|------|------------|
| [bin/agentic](bin/agentic) | Main CLI entry point — dispatches every subcommand (489 lines) |
| [bin/agentic-switch](bin/agentic-switch) | Toggles the backend (Anthropic ↔ Ollama) by rewriting `.agentic.conf` |
| [install.sh](install.sh) | Installer — pins Python 3.11.9 via pyenv, makes the venv, edits `~/.zshrc` |
| [lib/serve.py](lib/serve.py) | **The browser dashboard** — stdlib HTTP server + inline HTML/JS (2386 lines) |
| [lib/job_queue.py](lib/job_queue.py) | Queue API: submit, claim, state transitions, chains, accept, diffs, activity (995 lines) |
| [lib/queue.sh](lib/queue.sh) | Bash queue primitives (`queue_submit` / `queue_claim` / `queue_complete`) |
| [lib/worker.sh](lib/worker.sh) | Worker router — forks to `claude` CLI (cloud) or `ollama_worker.py` (local) |
| [lib/ollama_worker.py](lib/ollama_worker.py) | Local Ollama agent loop + **surgical repair loop** (1595 lines) |
| [lib/stream_parser.py](lib/stream_parser.py) | Parses the `claude` CLI's `stream-json` JSONL into a readable summary |
| [lib/apply.sh](lib/apply.sh) | Worktree creation, task-output validation, commit, accept/reject merge |
| [lib/claude-api.sh](lib/claude-api.sh) | Direct Anthropic Messages API client (curl) — caching, retries, usage |
| [lib/lang_profile.py](lib/lang_profile.py) | Detects project language and loads `profiles/<name>.json` |
| [lib/config.sh](lib/config.sh) · [init.sh](lib/init.sh) · [doc.sh](lib/doc.sh) · [plan.sh](lib/plan.sh) · [utils.sh](lib/utils.sh) | Config, project init, CLAUDE.md generation, agile planning, shared helpers |
| [agents/](agents/) | System prompts: `worker.txt` (cloud), `worker_local.txt` (local), `documenter.txt`, plus per-language `prompt_sections/<profile>[-local].txt` |
| [profiles/](profiles/) | Language profiles: `typescript.json`, `gameboy-c.json` |

---

## How a job flows

```
agentic submit "do X"           → JSON file in queue/pending/
agentic serve                   → dashboard at http://localhost:4080 (Run Worker / Run All)
  └─ POST /api/worker-stream     → spawns `agentic worker-once` (SSE-streamed to browser)
       └─ worker-once            → claim job → git worktree at ~/.agentic/worktrees/<id>/
            └─ worker.sh         → cloud: `claude` CLI  |  local: ollama_worker.py
                 → agent edits files, commits on branch agentic/<id>
                 → worker squashes to one commit "agentic: <id>", moves job to done/
agentic accept <id>             → merge agentic/<id> into base_branch
```

Jobs are **JSON files on disk**, moved between `queue/<state>/` directories. There is no database.

---

## Core concepts

- **Job** — a `TypedDict` with `id` (`j_YYYYMMDD_HHMMSS_xxxx`, [job_queue.py:190](lib/job_queue.py#L190)), a friendly `name` (adjective-noun), `request`, `target_repo` (absolute path), `model_hint`, `priority`, `base_branch` (captured at submit time), `parent_request_id` (chaining), `state_history` (append-only), and optional `job_type: "review"`, `profile`. Stored as `queue/<state>/{priority}_{YYYYMMDD}_{HHMMSS}_{id}.json`.
- **States** — `pending → running → done | failed | cancelled`, then `done → merged`; `running → abandoned` (manual, for stuck workers). Each state is a directory under `queue/`. `STATES = (pending, running, done, merged, failed, abandoned, cancelled)`.
  - ⚠️ `merged` and `abandoned` are **Python-only** — [queue.sh](lib/queue.sh) only knows `pending/running/done/failed/cancelled`.
- **Worktree** — each job runs in `~/.agentic/worktrees/<id>/` on branch `agentic/<id>`, so your working tree is never touched. Removed on accept/reject.
- **Chain** — `agentic submit --after <id>` (or the dashboard) sets `parent_request_id`. `queue_claim` blocks a job until its parent leaves `pending`/`running`. All commits in a chain go to the **root** job's branch (`_branch_job_id()` walks the chain up to the first non-review job).
- **Review job** — `job_type: "review"`, created from dashboard review comments (`POST /api/review`). Commits onto the **parent's** branch, not its own; cascade-merged when the parent is accepted. Has no `agentic/<own-id>` branch.
- **Accept Chain** — `accept_chain()` collects all `done` descendants, creates a staging branch **`agent-work/<YYYYMMDD>-<short_id>`** (`short_id = chain[0][-4:]`, [job_queue.py:442](lib/job_queue.py#L442)) off the base branch, merges each `agentic/<id>` in order, and marks them `merged`. You merge that staging branch into your own work when ready.
- **Profile** — language metadata auto-detected from repo files ([lang_profile.py](lib/lang_profile.py)); loaded from `profiles/<name>.json`. Defines build/lint commands, error-parsing regex, source extensions, and symbol-extraction patterns. Ships with `typescript` (default) and `gameboy-c`.

---

## The dashboard ([lib/serve.py](lib/serve.py))

- Plain **stdlib `http.server.ThreadingHTTPServer`** — no Flask/FastAPI ([serve.py:2362](lib/serve.py#L2362)).
- Binds **`127.0.0.1:4080`** by default; port is `sys.argv[1]` if given ([serve.py:2373](lib/serve.py#L2373), [:2380](lib/serve.py#L2380)). Localhost-only by design.
- The entire **HTML/CSS/JS UI is inline** in `HTML_TEMPLATE` (string in serve.py), served from `_serve_dashboard` with string substitution. There are no separate front-end asset files.
- Writes `PID:PORT` to `~/.agentic/serve.pid` on startup, unlinks on exit. `agentic serve stop` / `status` read this file.
- **Worker streaming is SSE**: `GET /api/worker-stream` spawns `agentic worker-once` in a daemon thread, buffers stdout in `_worker_log[]`, and broadcasts to clients. **Closing the browser does not stop the worker**; reconnecting replays the buffer.
- `DEFAULT_REPO` is the server's CWD at launch.
- **Mode is chosen at launch, not per request**: `AGENTIC_LOCAL=1` ⇒ model list comes from `/api/ollama-models`; otherwise from `/api/models` (Claude). The UI renders a local badge accordingly.

Key endpoints (all under `/api/`): `jobs`, `submit`, `worker-stream`, `worker-status`, `stop-worker`, `diff/<id>`, `activity/<id>`, `chain/<id>`, `job-full/<id>`, `accept`, `accept-chain`, `reject`, `cancel`, `abandon`, `delete`, `set-status`, `set-chain`, `review`, `apply-to-tree`. All delegate to [job_queue.py](lib/job_queue.py).

---

## Cloud worker (Claude Code)

- [worker.sh](lib/worker.sh) routes here when `AGENTIC_LOCAL` is unset. It shells out to the **`claude` CLI** ([worker.sh:76](lib/worker.sh#L76)):
  ```
  claude -p "$request" --system-prompt "$system_prompt" \
         --dangerously-skip-permissions \
         --allowedTools "Read,Edit,Write,Bash,Glob,Grep,LS" \
         --output-format stream-json --verbose [--model <model>]
  ```
  Output is piped through [stream_parser.py](lib/stream_parser.py) to render tool calls, files touched, and token usage.
- The system prompt is `agents/worker.txt` + the profile section `agents/prompt_sections/<profile>.txt` (no `-local` suffix in cloud mode).
- `--model` is added only when a model is set and isn't `"auto"`.
- [claude-api.sh](lib/claude-api.sh) is a **direct** Anthropic Messages API client (curl) with prompt caching (`anthropic-beta: prompt-caching-2024-07-31`, only for `anthropic.com`), 4-attempt exponential-backoff retries, and usage capture. It's used by helpers like `doc`/`plan`, not the main job loop.

---

## Local worker (Ollama) — and one correction

- [ollama_worker.py](lib/ollama_worker.py) talks to Ollama over the **OpenAI-compatible** endpoint `${OLLAMA_HOST}/v1/chat/completions` (default `http://localhost:11434`, [ollama_worker.py:32](lib/ollama_worker.py#L32), [:1177](lib/ollama_worker.py#L1177)) — **not** `/api/generate` or `/api/chat`. Streaming is **off** (`stream: false`).
- Model name = `AGENTIC_LOCAL_MODEL` (default `qwen2.5-coder:32b`, [ollama_worker.py:33](lib/ollama_worker.py#L33)), passed through **unmodified**.
- Tools (`Read/Edit/Write/Bash/Glob/Grep/LS`) are implemented as **Python functions**, not the `claude` CLI. `Bash` blocks destructive commands (`rm -rf`, `killall`, `pkill`).
- **Surgical repair loop**: after the main loop, it runs the project build; on failure it fixes **one error per round** in **Edit-only** mode, locks edits to the files named in the error output, checkpoints via `git stash`, and **reverts + escalates** if the error count goes up. Max **5 rounds** (`MAX_REPAIR_ROUNDS`, [:36](lib/ollama_worker.py#L36)). A spiral guard hard-stops after 5 consecutive `Bash` calls with no Read/Edit/Write.
- **Context compression**: when an estimate exceeds `AGENTIC_CONTEXT_BUDGET` it replaces old `Read` results with symbol maps (TS exports / CSS selectors / truncation), keeping the most recent `AGENTIC_KEEP_RECENT_TURNS` (15) turns.

> ⚠️ **Correction to a common description.** The local setup does **not** automatically "create a local image scaled for the model with `:latest` added to the name." There is **no `ollama create`, no Modelfile, no `num_ctx`, and no `:latest` tagging anywhere in the code** (verified by grep across the repo, and by reading [ollama_worker.py](lib/ollama_worker.py)). Building a context-scaled Ollama image (`ollama create qwen-coder -f Modelfile` with `PARAMETER num_ctx …`) is a **manual, one-time step documented in [README.md](README.md)** that the *user* performs; the worker just reads the resulting model name from `AGENTIC_LOCAL_MODEL`. The only context handling the code does is the Python-side budget/compression above.

---

## Config & backend switching

Settings live in `~/.agentic/.agentic.conf` (sourced from `~/.zshrc`); env vars override the file ([config.sh](lib/config.sh)). See [.agentic.conf.example](.agentic.conf.example).

| Key | Default | Notes |
|-----|---------|-------|
| `AGENTIC_LOCAL_MODEL` | `qwen2.5-coder:32b` | Ollama model for local mode |
| `AGENTIC_CONTEXT_BUDGET` | `24000` | Token budget before old reads are compressed; example conf and `ollama_worker.py` default agree |
| `AGENTIC_KEEP_RECENT_TURNS` | `15` | Turns kept verbatim before compression |
| `ANTHROPIC_API_KEY` / `AGENTIC_MODEL` / `ANTHROPIC_BASE_URL` | unset | Cloud/Anthropic settings |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint |

- [agentic-switch](bin/agentic-switch) flips between `anthropic` and `ollama`, rewriting `.agentic.conf` (chmod 600). Switching to Ollama **stashes `ANTHROPIC_API_KEY` as a comment** so switching back is prompt-free. Backend is detected by whether `ANTHROPIC_BASE_URL` contains `anthropic.com`.
- `agentic-switch` writes the Anthropic model as `claude-opus-4-8`; the dashboard's `FALLBACK_MODELS` list ([job_queue.py](lib/job_queue.py)) offers `claude-opus-4-8`, `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`. Model ids drift fast — **verify the current id against the Claude API docs/skill before hardcoding a new one** rather than guessing.
- ⚠️ The live (gitignored, never-committed) `~/.agentic/.agentic.conf` contains a real `ANTHROPIC_API_KEY`. Don't print, log, or echo it; don't paste config contents into commits, PRs, or external services.

---

## CLI reference (selected)

```
agentic submit "<request>" [--repo PATH] [--model-hint H] [--priority N] [--after JOB-ID]
agentic inbox [--state STATE] [--json]      # list jobs
agentic cancel <id>                          # cancel a pending job
agentic worker-once                          # claim + run exactly one job
agentic serve [PORT]                         # cloud dashboard (default :4080)
agentic serve --local[=MODEL] [MODEL]        # local Ollama dashboard
agentic serve stop | status
agentic accept <id> | reject <id>            # merge / discard a job's branch
agentic switch anthropic|ollama|status
agentic doc | doc-gen | plan                 # edit / generate CLAUDE.md, agile plan
agentic --version | --help
```

---

## Setup / install notes

- [install.sh](install.sh) **requires `pyenv`** and pins **Python 3.11.9**, then builds the venv at `~/.agentic/venv/`. It also needs `jq` (used by `init.sh` to merge `.vscode/settings.json`).
- It appends to **`~/.zshrc`** (not `~/.bashrc`).
- Cloud mode needs the `claude` CLI on `PATH`; local mode needs `ollama serve` running plus a pulled model.
- All Python is invoked as `${AGENTIC_HOME}/venv/bin/python3 …` — use that interpreter, not the system one.

---

## Gotchas to keep in mind

- **`base_branch` is frozen at submit time.** If the repo's HEAD moves, `accept_job` can fail on a dirty tree — use Accept Chain (staging branch) or stash first.
- **`set_chain` doesn't validate the DAG** — circular chains are possible if set carelessly.
- **`queue_claim` is the only atomic op** (a `mv`/rename). The chain-dependency check is non-atomic; concurrent workers can race.
- **Diffs persist after cleanup**: `~/.agentic/diffs/<id>.diff` is cached (≤512 KB) so the dashboard still shows changes after the worktree is gone.
- **Activity comes from `~/.agentic/logs/<id>.jsonl`** (or the worktree's `.agent_log.jsonl`) — files modified/read, tool calls, build/lint results, token counts.
- **Crashed workers leave worktrees and branches behind.** Use `abandon` then `delete` to clean up.

---

## Working in this repo

- Match the surrounding style: bash libs use `_prefixed` helper functions and `source` each other from `lib/`; Python modules are stdlib-only where possible (no web framework, `http.server` + `urllib`).
- The dashboard UI is one big inline string in [serve.py](lib/serve.py) — edit HTML/CSS/JS there, not in separate files.
- When touching anything that names a Claude/Anthropic model, **verify the id against current Claude API docs first** — model ids in any repo go stale fast. As of this writing the repo uses `claude-opus-4-8` / `claude-sonnet-4-6` (current); update them when newer ids ship rather than copying old strings.

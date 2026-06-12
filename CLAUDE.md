# CLAUDE.md

Guidance for AI agents and engineers working in **agentic** — an async dev-task queue. You submit a request, an agent runs it in an isolated git worktree, and you review the diff and merge what you like. Originally a CLI toolset; the primary interface is now a browser dashboard, and the primary install is Docker.

> This file documents **what the code actually does today** (verified against source, June 2026), not aspirational behavior. Where a README and the code disagree, the code wins — flagged with ⚠️.

---

## What this is

- A bash CLI (`bin/agentic`) that dispatches to Python (`lib/*.py`) and bash libraries (`lib/*.sh`).
- **No `package.json`** at the root — this is not a Node project. It's bash + Python 3.11, stdlib-only (no web framework, no DB).
- Version: `agentic v2.0.0` ([bin/agentic:521](bin/agentic#L521)).
- **Backend is per-job**, chosen when you submit: **Local** (Ollama) or **Cloud** (Claude Code's `claude` CLI). One running server dispatches *both* concurrently.
- **Primary install is Docker** (all-in-one image; Ollama runs on the host). A native installer (`install.sh`, pyenv + venv) still exists for contributors.

`AGENTIC_HOME` is the **state** dir (queue/worktrees/diffs/logs/settings/secrets); `AGENTIC_APP` is the **app source** tree. Natively both default to `~/.agentic`; in Docker `AGENTIC_APP=/opt/agentic` (baked, never mounted) and `AGENTIC_HOME` is the mounted state dir.

---

## Repo layout

| Path | What it is | Lines |
|------|------------|-------|
| [bin/agentic](bin/agentic) | Main CLI entry — dispatches every subcommand (incl. `claim`, `worker-once --id/--backend`, `serve`) | 570 |
| [bin/agentic-switch](bin/agentic-switch) | Legacy backend toggle (Anthropic ↔ Ollama) via `.agentic.conf` — superseded by per-job backend + Settings | 139 |
| [lib/serve.py](lib/serve.py) | **The dashboard** — stdlib HTTP server + the concurrency dispatcher + all `/api/` endpoints | 2372 |
| [lib/job_queue.py](lib/job_queue.py) | Queue API: submit, claim helpers, state transitions, chains, accept/accept-chain, diffs, merge-conflict probe, activity | 1320 |
| [lib/slots.py](lib/slots.py) | **Per-backend slot pool** (`POOL`) — the concurrency ceiling for local + cloud work | 166 |
| [lib/queue.sh](lib/queue.sh) | Bash queue primitives — `queue_submit`, **backend-aware `queue_claim`**, `queue_complete`, chain-root walk | 314 |
| [lib/worker.sh](lib/worker.sh) | Worker router — forks to the `claude` CLI (cloud) or `ollama_worker.py` (local) | 131 |
| [lib/ollama_worker.py](lib/ollama_worker.py) | Local Ollama agent loop + surgical repair loop + `run_ask` planning path | 2553 |
| [lib/planner.py](lib/planner.py) | Planning-channel engine: tiered grounding, job derivation, 2-stage anchor verification | 1277 |
| [lib/channels.py](lib/channels.py) | Planning channels + threads + proposals persistence | 637 |
| [lib/settings.py](lib/settings.py) | **Settings** (`settings.json` knobs) + **secrets** (`secrets.json`, 0600) — the config source of truth | 382 |
| [lib/stream_parser.py](lib/stream_parser.py) | Parses the `claude` CLI's `stream-json` JSONL into a readable summary | 114 |
| [lib/apply.sh](lib/apply.sh) | Worktree creation, task-output validation, commit | 510 |
| [lib/claude-api.sh](lib/claude-api.sh) | Direct Anthropic Messages API client (curl) — used by `doc`/`plan`, not the job loop | 404 |
| [lib/diff_guard.py](lib/diff_guard.py) | Risk classifier (command/path) shared by the worker's Bash gate + the accept anomaly gate | 214 |
| [lib/lang_profile.py](lib/lang_profile.py) | Detects project language, loads `profiles/<name>.json` | 105 |
| [lib/config.sh](lib/config.sh) · [init.sh](lib/init.sh) · [doc.sh](lib/doc.sh) · [plan.sh](lib/plan.sh) · [utils.sh](lib/utils.sh) | Legacy config, project init, CLAUDE.md gen, agile planning, shared helpers | — |
| [lib/static/](lib/static/) | **The UI** — `app.css`, `app.js`, `job.css`, `job.js`, served via `/static/` (NOT inline in serve.py) | — |
| [agents/](agents/) | System prompts: `worker.txt`, `worker_local.txt`, `planner.txt`, `planner_derive.txt`, `documenter.txt`, `prompt_sections/<profile>[-local].txt` | — |
| [profiles/](profiles/) | Language profiles: `typescript.json`, `gameboy-c.json` | — |
| [docker/](docker/) | `Dockerfile`, `docker-compose.yml`, `entrypoint.sh`, `setup.sh` (wizard), `up.sh`/`down.sh`, `README.md` | — |

---

## How a job flows

```
agentic submit "do X" [--model-hint local|remote]   → JSON file in queue/pending/
agentic serve                                         → dashboard at http://localhost:4080
  └─ Run Worker / Run All  →  POST /api/run-worker | /api/run-all   (no backend = BOTH pools)
       └─ dispatcher _pump()  (serve.py, daemon, self-rearming)
            ├─ POOL.try_acquire(backend)              # ceiling: local=2, cloud=4 (settings)
            ├─ agentic claim --backend <b> --exclude-chain-roots …   # atomic mv → running/
            └─ agentic worker-once --id <id> --backend <b>           # skips its own claim
                 └─ worker.sh   →  cloud: `claude` CLI   |   local: ollama_worker.py
                      → agent edits files in ~/.agentic/worktrees/<id>/, commits on agentic/<id>
                      → squashes to one commit, moves job to done/
agentic accept <id>          → merge agentic/<id> into the CURRENT branch (HEAD)
```

Jobs are **JSON files on disk**, moved between `queue/<state>/` directories. There is no database. The dispatcher keeps **both pools busy at once** — up to `local_max + cloud_max` workers concurrently.

---

## Core concepts

- **Job** — a `TypedDict` ([job_queue.py](lib/job_queue.py)): `id` (`j_YYYYMMDD_HHMMSS_xxxx`), friendly `name` (adjective-noun), `request`, `target_repo` (absolute), **`model_hint`** (now the **backend selector** — see below), `priority`, `base_branch` (captured at submit), `parent_request_id` (chaining), `submitted_at`/`submitted_by`, append-only `state_history`, `summary`, and optional `job_type: "review"`, `reviews`, `profile`/`profile_display`. `_state` and `session` are injected at read time, not stored. (`read_jobs` may also annotate `merge_conflict`/`conflict_files`/`chain_gated`/`resolved_model` for the UI.)
- **States** — `pending → running → done | failed | cancelled`, then `done → merged`; `running → abandoned` (manual). Each is a directory under `queue/`. `STATES = (pending, running, done, merged, failed, abandoned, cancelled)` ([job_queue.py:131](lib/job_queue.py#L131)).
- **Per-job BACKEND** — a job's `model_hint` is the backend marker, **not** a model name: `local` → local pool, `remote` → cloud pool, `auto`/blank/anything → **local**. Resolved by **`_backend_of`** ([serve.py](lib/serve.py)) and **`_queue_job_backend`** ([queue.sh](lib/queue.sh)) — the two MUST agree (both mode-free; auto→local). The concrete model comes from `AGENTIC_LOCAL_MODEL` / `AGENTIC_MODEL` at run time, never from `model_hint`. **There is no global "mode" setting** — it was removed. `set_backend(job_id, backend)` flips a job's backend while it's **pending only** (`/api/set-backend`).
- **Worktree** — each job runs in `~/.agentic/worktrees/<id>/` on branch `agentic/<id>`, so your working tree is untouched. Removed on accept.
- **Chain** — `agentic submit --after <id>` sets `parent_request_id`. A child is not claimed until its parent leaves `pending`/`running` — **this holds across backends** (a cloud child waits for its local parent). All commits in a chain go to the **root** job's branch. Chains may **mix backends** (a child inherits its parent's by default).
- **Review job** — `job_type: "review"`, created from dashboard review comments (`POST /api/review`). Commits onto the **parent's** branch; has no own branch.
- **Accept** — `accept_job` merges `agentic/<id>` into **whatever branch is currently checked out** (HEAD) — *not* the frozen `base_branch`, and *not* a staging branch. No branch switch, no extra step. Detached HEAD is refused. ⚠️ The `accept_job` docstring still says "into base_branch" — stale; the code merges into `current`.
- **Accept Chain** — `accept_chain` merges every done job in the chain, **in order, into the current branch** (no `agent-work/` staging branch anymore). The first conflict stops there for you to resolve.
- **Merge-conflict pre-detection** — `read_jobs` flags a `done` job whose accept would now conflict with the moved base (via `git merge-tree --write-tree`, SHA-pair cached) as `merge_conflict` + `conflict_files`. The UI shows a red badge; **"Resolve merge"** (`review_job` → `git apply --3way`) writes conflict markers into your tree to resolve in the IDE.
- **Profile** — language metadata auto-detected from repo files ([lang_profile.py](lib/lang_profile.py)); `profiles/<name>.json`. Ships with `typescript` (default) and `gameboy-c`.

---

## The concurrency dispatcher (serve.py)

The old "one worker, browser-driven recursion" model is gone. A **server-side daemon dispatcher** fills slots for both backends.

- **`lib/slots.py` — `POOL`**: a per-backend non-blocking counting semaphore. API: `try_acquire(backend)→bool`, `release(backend)`, `configure(local_max, cloud_max)`, `snapshot()`. Defaults `local_max=2` (`ollama_num_parallel`), `cloud_max=4` (`cloud_max_workers`). Own leaf lock — never nested under other locks.
- **`_pump()`** — idempotent, self-rearming. For each backend, while there's demand (`_drain` latch or `_pending_n` one-shot) and a free slot: `try_acquire` → `_claim` → `_spawn`. Each worker's `_run`, on finish, releases its slot and calls `_pump()` to refill. No busy-loop.
- **Race discipline**: `_claim` (which shells out to `agentic claim`) runs **OUTSIDE `_disp_lock`** so a slow claim can't freeze status/stop/other pumps; the in-memory acquire+register is under the lock. **Single-owner teardown**: `_finalize_worker`'s atomic `_active.pop` is the ownership token, so `_run` and the watchdog can't double-release/double-emit.
- **Chain serialization**: `queue_claim --exclude-chain-roots <csv>` skips any job whose chain root is already in flight, so two siblings of one chain never run at once.
- **Keyed buffers**: the singleton `_worker_*` is replaced by per-job dicts `_w_log/_w_done/_w_rc/_w_cond/_w_meta/_w_gc` (mirrors the `_ask_*` planning pattern). `_w_final` preserves a job's terminal rc across buffer GC.
- **Watchdog** (10 s daemon): dead-proc reaper (reaps a crashed worker via `_finalize_worker`), slot-leak reaper (reclaims a slot only when the discrepancy **persists** across two ticks — avoids a teardown-window double-release; counts `_active` workers **plus** `_plan_held` planning slots), buffer GC (60 s after finish).
- **Planning slots**: chat (`ask`) and `derive` also consume a pool slot via `_plan_acquire`/`_plan_release` (a local chat competes with a local worker for Ollama). `_plan_held` keeps the watchdog from reaping a live planning slot. A running chat/derive can be stopped via `/api/ask-cancel` (kills the subprocess group; it runs `start_new_session=True`).

---

## The dashboard ([lib/serve.py](lib/serve.py))

- Plain **stdlib `http.server.ThreadingHTTPServer`** — no Flask/FastAPI. Binds `127.0.0.1:4080` natively (`0.0.0.0` in Docker); port is `argv[1]`.
- The **UI lives in `lib/static/`** (`app.css`, `app.js`, `job.css`, `job.js`), served via the `/static/` route from `AGENTIC_APP`. It is **no longer inline** in serve.py. `window.AGENTIC_CFG` carries inline config; IDE design tokens in `:root`. The header is **two rows** (info: title + model badges + pool chip + age; controls: view toggle + run buttons + gear).
- **Streaming is SSE, keyed**: a **dispatch-index stream** (`GET /api/dispatch-stream?since=`) tells a (re)connecting browser which workers are in flight; **per-job streams** (`GET /api/worker-stream/<id>?cursor=`) carry one worker's log. Reconnecting re-attaches to **all** in-flight workers. Closing the browser does not stop a worker.

Key endpoints (all under `/api/`):
- **Jobs / dispatch**: `jobs`, `submit`, `run-worker`, `run-all` (no backend = both pools), `stop-worker` (`{job_id}` or `{all:true}`), `worker-status`, `pool`, `set-backend`, `set-chain`, `set-status`.
- **Streaming**: `dispatch-stream`, `worker-stream/<id>`, `ask-stream`, `ask-reconnect`, `ask-cancel`.
- **Review / merge**: `diff/<id>`, `activity/<id>`, `chain/<id>`, `job`/`job-full`, `accept`, `accept-chain`, `reject`, `cancel`, `abandon`, `delete`, `apply-to-tree`, `review`, `peek`.
- **Channels**: `channels`, `channel/…` (+ derive + proposal), `repos`, `browse`.
- **Config**: `settings`, `secrets` (write-only), `models`, `ollama-models`.

---

## Cloud + local workers

**Cloud (Claude Code)** — [worker.sh](lib/worker.sh) routes here when `AGENTIC_LOCAL` is unset. It shells out to the **`claude` CLI**:
```
claude -p "$request" --system-prompt "$system_prompt" \
       --dangerously-skip-permissions \
       --allowedTools "Read,Edit,Write,Bash,Glob,Grep,LS" \
       --output-format stream-json --verbose [--model <model>]
```
- Authenticates via `ANTHROPIC_API_KEY` from `secrets.json`, injected per-spawn by the dispatcher; the model comes from `AGENTIC_MODEL` (`--model` is added only when set and not `"auto"`). **`model_hint` (`local`/`remote`/`auto`) is a backend marker, not a model name** — worker.sh blanks those markers so it never passes `--model remote` (which 404s).
- **Root guard**: the CLI refuses `--dangerously-skip-permissions` as root, so the worker aborts loudly if uid 0. In Docker the entrypoint gosu-drops to `HOST_UID:HOST_GID`, so workers run non-root.
- worker.sh **unsets `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`** on the cloud path so the CLI dials `api.anthropic.com` (defense against the local-backend's Ollama endpoint leaking in).
- [claude-api.sh](lib/claude-api.sh) is a separate direct Messages API client (curl) used by `doc`/`plan`, not the job loop.

**Local (Ollama)** — [ollama_worker.py](lib/ollama_worker.py) talks to Ollama over the **OpenAI-compatible** endpoint `${OLLAMA_HOST}/v1/chat/completions` (default `http://localhost:11434`) — **not** `/api/generate`. Streaming is off. Model = `AGENTIC_LOCAL_MODEL` (default `qwen-coder:latest`), passed through unmodified. Tools are Python functions; `Bash` blocks destructive commands.
- **Surgical repair loop**: after the main loop it runs the build; on failure it fixes **one error per round** (Edit-only, edits locked to the error's files, `git stash` checkpoint, revert+escalate on regression). Max **5 rounds** (`MAX_REPAIR_ROUNDS`).
- **Context compression**: replaces old `Read` results with symbol maps once an estimate exceeds `AGENTIC_CONTEXT_BUDGET`, keeping the most recent `keep_recent_turns` (15) turns.
- **`run_ask`** (planning path): forces a final tool-free synthesis turn if the agent burns its turn budget reading, so a planning answer isn't truncated to its preamble.

---

## Config & secrets

**Config lives in `settings.json` + `secrets.json`** under `AGENTIC_HOME`, managed by [settings.py](lib/settings.py). The dashboard's **Settings gear is the source of truth**; env vars are only a fallback/seed. The legacy `~/.agentic/.agentic.conf` is a **one-time migration source** (`migrate_from_conf_if_needed`), not the live config.

- **`settings.json`** — knobs via the `SCHEMA`; `load()` / `get(key)` / `save()`. Selected knobs (key · default · env):
  - `local_model` · `qwen-coder:latest` · `AGENTIC_LOCAL_MODEL`
  - `cloud_model` · `auto` · `AGENTIC_MODEL`
  - `ollama_num_parallel` · `2` · `OLLAMA_NUM_PARALLEL` (sizes agentic's **local dispatch pool** — NOT sent to Ollama; match it to the host `ollama serve`'s `OLLAMA_NUM_PARALLEL`)
  - `cloud_max_workers` · `4` · `CLOUD_MAX_WORKERS` (cloud dispatch pool size)
  - `pause_chain_for_review` · `False` · `AGENTIC_CHAIN_GATE` (gate a chain link until its parent is accepted)
  - `context_budget` · `24000`, `keep_recent_turns` · `15`, `max_turns` · `60`, `compress_margin` · `4000`
  - `planning_max_turns` · `8`, `planning_default_mode` · `local`, `planning_default_model` · `""`
  - `ollama_keep_alive` · `30m`, `ollama_max_loaded` · `3`, `ollama_timeout` · `1800`
  - `read_max_lines` · `400`, `bash_max_chars` · `4000`, `grep_max_chars` · `4000`
  - `default_repo` · `""` (the in-app picker, confined to `BROWSE_ROOT`)
- **`secrets.json`** (mode 0600) — `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, via `get_secret`/`set_secret`. The allowed secret keys are exactly those two. `secrets_status()` returns booleans only — the key is **never** echoed to the browser. `get_secret` resolves secrets.json first, then env.
- ⚠️ `secrets.json`, `settings.json`, `serve.pid`, `.agentic.conf`, `docker/.env` are gitignored and never committed; `secrets.json` is never baked into the Docker image. **Don't print, log, echo, or paste the API key anywhere.**

**Source vs state split**: `AGENTIC_APP` (app source — `/opt/agentic` baked in Docker, `~/.agentic` natively) vs `AGENTIC_HOME` (mounted state). `AGENTIC_PYTHON` is the interpreter (the baked venv). Compose reads `AGENTIC_STATE_DIR` (named differently from `AGENTIC_HOME` so a shell-exported `AGENTIC_HOME` can't shadow it).

**Model ids drift fast.** Current Claude ids in `FALLBACK_MODELS` ([job_queue.py:1178](lib/job_queue.py#L1178)): `claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5`. **Verify the current id against the Claude API docs/skill before hardcoding a new one** rather than copying old strings.

---

## Planning channels ([planner.py](lib/planner.py) + [channels.py](lib/channels.py))

- **Per-repo channels** with named **threads**; a thread has its own **planning backend** (`planning_mode` local/cloud — decoupled from a job's backend) and optional model.
- **Tiered grounding**: a question first tries the free symbol index (zero model turns); only an ambiguous one escalates to a read-only agent (`run_agent`, which spawns `ollama_worker.py --ask` locally or the `claude` CLI for cloud).
- **Job derivation**: `derive` turns a conversation into a job proposal, with **2-stage anchor verification** (existence gate + advisory relevance). Jobs carry only `.request`, never chat context. The proposal whitelist must carry `backend`/`model_hint` for per-job backend to survive to submit.
- **Streaming**: `ask`/`derive` run in daemon threads, stream over `_ask_*` keyed buffers; `/api/ask-reconnect` re-attaches after navigating away; `/api/ask-cancel` stops a run. Both acquire a `POOL` slot (planning competes with workers for a backend).

---

## Docker

- **All-in-one image**: `serve.py` runs as **pid 1 via `exec gosu HOST_UID:HOST_GID`** (the entrypoint). The **`claude` CLI is baked into the image** (cloud works in-container); **Ollama runs on the host** (`host.docker.internal:11434`) — there is no ollama in the image.
- **Identity mounts** + the **source/state split**: the baked `/opt/agentic` is never mounted; the host state dir mounts to `AGENTIC_HOME`; container `HOME = $AGENTIC_STATE_DIR/home` so npm cache + git config are writable.
- The **entrypoint is create-only** (no `rm`/`mv`/`chown -R`/`ln -s`; a Dockerfile build-time tripwire enforces this).
- The wizard (`docker/setup.sh`) writes `docker/.env` (project dir, state dir, `HOST_UID`/`HOST_GID`, port, Ollama URL) and scaffolds the state dir. The API key is set **in the UI**, not in `.env`.
- See [README.md](README.md) "Before you start" and [docker/README.md](docker/README.md).

---

## Gotchas to keep in mind

- **`base_branch` is frozen at submit time**, but **Accept ignores it** — it merges into the *current* branch (HEAD). So the old "accept fails on a moved base" gotcha is gone; the new rule is "Accept lands on whatever branch you're checked out on."
- **`queue_claim` is the only atomic op** (a `mv`/rename). The chain-dependency check and the `--exclude-chain-roots` check are non-atomic; the atomic `mv` still prevents double-claims, but two pumps can race on which job each gets.
- **`_backend_of` and `_queue_job_backend` must stay in sync** — they're the two halves of backend routing (Python dispatcher + bash claim filter). If they disagree, a job's pool and its claim filter diverge (this exact bug bit us via a stale `mode` read).
- **Diffs persist after cleanup**: `~/.agentic/diffs/<id>.diff` is cached so the dashboard still shows changes after the worktree is gone.
- **Activity** comes from `~/.agentic/logs/<id>.jsonl` (or the worktree's `.agent_log.jsonl`).
- **Crashed workers** leave worktrees and branches behind; the watchdog reaps the *slot* but the worktree/branch remain — use `abandon` then `delete` to clean up.
- **Two `OLLAMA_NUM_PARALLEL`s**: the host `ollama serve` env var is what actually lets Ollama run requests concurrently; the Settings knob of the same name only sizes agentic's local dispatch pool. Match them.

---

## Working in this repo

- Match the surrounding style: bash libs use `_prefixed` helpers and `source` each other from `lib/`; Python is stdlib-only where possible (`http.server` + `urllib`, no framework).
- **The UI is in `lib/static/`** — edit `app.css`/`app.js`/`job.css`/`job.js` there, not inline in serve.py (that note in older docs is stale).
- The dispatcher's lock discipline is load-bearing: keep slow subprocesses (claim) **out** of `_disp_lock`, and keep teardown single-owner (`_finalize_worker`). When in doubt, mirror the `_ask_*` pattern.
- **Verify Claude/Anthropic model ids** against current Claude API docs before hardcoding — they go stale fast.
- This file is verified June 2026 against source. If you change architecture, update it (or regenerate it from the code).

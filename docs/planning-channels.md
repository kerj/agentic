# Design: Planning Channels

> Status: **proposed** (design only — no code yet). Target branch: `bug-fixes`
> (current) or a fresh `feat/planning-channels` off it.

A conversational **planning surface** in the dashboard where you ask questions
about your codebase, get answers **grounded in your real code**, and derive
well-specified jobs from the conversation. It attacks the main workflow friction:
today you write a cold job description and hope the agent infers your conventions;
with this, you've already surfaced the pattern and hand the agent a job that
*points at real code*.

This design is the synthesis of a 4-way design exploration (winner: "Channel as
Read-Agent Pool", 40/50), with three refinements decided by the user — see
[Decisions](#decisions-locked).

---

## The core bet

**A question is a job that may only read.** Instead of prompting a model to "be
grounded," a question runs the existing **read-agent loop** (Read/Grep/Glob/LS)
and **citations are harvested from the files it actually opened**. Grounding is
correct *by mechanism*, not by prompt discipline. This plays to the tool's real
strength — the agent loop — rather than building new retrieval machinery.

A **tiered cost gradient** keeps it cheap: trivial "what exists?" questions answer
from a symbol index for **zero model turns**; only "how/why" escalates to the
agent. The UI shows the cost so you learn the cheap path.

---

## Decisions (locked)

1. **Per-repo container, multiple named threads.** A repo has one durable
   *channel* (keyed by repo path); inside it you create multiple **threads** so
   context stays scoped per topic instead of one giant chat. Reopening a repo
   finds prior threads + their accumulated citations.

2. **Threads NEVER leak into jobs.** A derived job carries **only its request
   string** (with baked `file:line` anchors) — no thread, no transcript, no
   channel reference. When the runner executes the job, the conversation is *not*
   in its context. The chat is a scoping aid, not a context source. This is
   already enforced by the queue: the worker reads only `.request`
   ([bin/agentic:215,334](../bin/agentic)) — we simply never add a transcript
   field to the job.

3. **Broaden the symbol index for existing languages** (TypeScript, gameboy-c) —
   richer than today's export-only map (add internal functions/types/classes).
   More languages come later; non-indexed cases fall back to grep → agent.

---

## Data model (JSON-on-disk, mirrors `queue/`)

New state dir: `AGENTIC_HOME/channels/`. No database, no migrations —
`channels_init()` just `mkdir`s the tree, exactly like `queue_init()`.

```
channels/
  <cid>/                         cid = c_<sha1(realpath(repo))[:12]>  (per-repo, durable)
    channel.json                 header: repo, profile, base_branch, created_at,
                                   index_head_sha, default planning backend
    <tid>.thread.json            per-THREAD header: id, name (adjective-noun),
                                   title (first question), planning_mode (local|cloud),
                                   planning_model, created_at, updated_at
    <tid>.jsonl                  append-only transcript (SAME shape as logs/<id>.jsonl
                                   so existing activity renderers apply):
                                     {"role":"user","text":...,"at":...}
                                     {"role":"assistant","text":...,"grounding":"index"|"agent",
                                       "turns":N,"citations":[{file,start,end,sha,why}],
                                       "tokens":{in,out},"at":...}
                                     {"role":"tool","name":"Read","input":{...},"at":...}  (display)
                                     {"role":"draft","proposal_id":...,"at":...}
    <tid>.citations.jsonl        deduped evidence locker for the thread
    proposals/<pid>.json         derived job set (editable, survives reload):
                                     {proposal_id, summary, status:"draft"|"submitted",
                                      jobs:[{seq,title,request,depends_on,anchors}],
                                      submitted_job_ids:[]}
  <cid>.symbols                  cached symbol map "file: sym1, sym2"; rebuilt only
                                   when git HEAD moves (cheap `git rev-parse HEAD`)
```

- **Channel** = per-repo container (one per repo path).
- **Thread** = a scoped conversation inside a channel. Multiple per channel.
- The symbol index is per-**channel** (per repo), shared by all its threads.
- Atomic writes (`temp + os.replace`) and append-only `.jsonl` — direct copy of
  the `job_queue.py` pathlib patterns.

---

## Grounding flow (tiered, deterministic gate first — `lib/planner.py`)

The decision of *cheap vs escalate* is **pure Python (regex), never an LLM** — so
it's free and inspectable.

**Step 0 — Symbol index** (no model, cached per repo). Build a `file: sym1, sym2`
map. Port the Python body of `_build_repo_map()`
([utils.sh:64](../lib/utils.sh)) into `planner.py:build_symbol_map(repo, profile)`
— **broadened** (per decision 3) to extract internal functions/types/classes for
TS and gameboy-c, not only named exports. Cache to `<cid>.symbols`, stamped with
git HEAD; rebuild only when HEAD moves.

**Step 1 — Classify** (pure regex, free):
- **Index-answerable:** "what/which … (export|function|component|hook|type|file)s
  in/under `<path>`", "list …", "where is `<Symbol>`", "does `<Symbol>` exist".
- **Escalate:** "how/why/should/what happens when/explain/trace/what calls",
  anything naming *behavior*, OR an index attempt returning 0 or >40 hits
  (ambiguous → cheap path can't be confident).

**Step 2A — Index path (Tier 0, zero model turns):** answer from the map (+ at
most one grep for `file:line`). Badge: **"index • free"**.

**Step 2B — Agent path (escalation):** dispatch a **read-only** agent with the
**curated efficient tool set** (below) with the question as its request and the
symbol map as seed context. `max_turns = planning_max_turns` (default 8, vs jobs'
60). The final assistant text is the answer. **Citations are harvested from the
agent's actual read calls** and appended (deduped) to the thread's citation
locker. Badge: **"read N files · K turns"**.

**Step 3 — Confidence backstop:** escalation *is* the confidence mechanism. An
index answer the user follows up on with "how/why" naturally re-escalates. A "Dig
deeper" toggle forces the agent path for any under-answered question.

### Curated efficient read tools (`TOOLS_PLANNING`)

The planning agent is read-only **and token-efficient**: instead of brute-force
`Read whole file` (≈400 lines to answer one question), it gets purpose-built
tools that return *less but answer more*. Each has a heuristic **expected savings
vs full Read**, surfaced as a percent ("saved ~20%") so cost is legible without
real-time accounting.

| Tool | Returns | Answers | Heuristic savings vs Read |
|------|---------|---------|---------------------------|
| `Outline(file)` | symbol map of one file (signatures/exports, no bodies) | "what's in here" | ~85% |
| `Signature(symbol)` | just the declaration line(s) | "what's the shape of X" | ~95% |
| `ReadSymbol(file, name)` | one function/class body, not the whole file | "how does X work" | ~60% |
| `Usages(symbol)` | grep hits + one line of context each | "what calls X" | ~70% |
| `Read/Grep/Glob/LS` | (existing) | fallback when the above don't fit | — |

- **Read-only, no Bash.** This supersedes the earlier "read-in-place, no tools"
  option: we *do* want tools, just a curated efficient read set — never Edit or
  Bash. (Reverses open-question 1's first framing.)
- The agent is **nudged by prompt** to prefer the cheapest tool that answers, with
  full `Read` as the explicit fallback. The model isn't *forced* — see the
  tipping-point model under [Cost signals](#cost-signals-suggestion-not-enforcement).
- Implementation: `Outline`/`Signature` reuse the symbol-map regexes;
  `ReadSymbol` is a symbol-bounded slice of `tool_read`; `Usages` is `tool_grep`
  + context lines. All built on existing primitives (~60 lines total in
  `planner.py`).

---

## Model selection (per thread, decoupled from job-run mode)

Each thread stores `planning_mode` (local|cloud) + `planning_model`, chosen at
create and editable live. **Plan with cloud Opus while jobs run on local Ollama,
or vice-versa** — this is the key power feature. The planning path is read-only
and separate from the global `mode` (which only governs job runs), so they don't
interfere and need no restart.

- **Local:** spawn `python3 lib/ollama_worker.py --ask` as a **subprocess** with
  `AGENTIC_SANDBOX_ROOT=<repo>`, `cwd=<repo>`. (Subprocess is required:
  `SANDBOX_ROOT` is import-time captured at [ollama_worker.py:69](../lib/ollama_worker.py),
  so in-process reuse in the long-lived dashboard would race a global.) Reuses
  `run_agent_loop` with a read-only tool set.
- **Cloud:** the read-agent **is the `claude` CLI** — already a tool loop with
  `--allowedTools "Read,Grep,Glob,LS"` + `--output-format stream-json`, piped
  through the existing `stream_parser.py`, exactly like the cloud job worker
  ([worker.sh:76](../lib/worker.sh)). **Deliberate non-reuse:** `claude-api.sh`
  is a single-shot curl with no tool loop — it can't ground in files, so cloud
  planning uses the CLI, not `claude-api.sh`. This avoids hand-rolling an
  Anthropic tool-use loop (the hidden cost that sank two explored designs).

Both model lists (cloud + local) are shown to the picker regardless of global
mode. `ANTHROPIC_API_KEY` is resolved server-side via `settings.get_secret()`,
passed in the child env, **never sent to the browser**.

New settings knobs: `planning_max_turns` (8), `planning_default_mode`,
`planning_default_model`. **No per-thread token/cost ceiling** —
`planning_max_turns` is the only cap (open-question 4: left out as too much).

---

## Cost signals (suggestion, not enforcement)

Cost is **always a suggestion the user can override**, never an automatic
downgrade or a block.

- **Local-vs-cloud is recommended, not enforced.** The UI may hint ("this looks
  like a cheap lookup — the index/local model can handle it"), but it never
  auto-switches your thread's backend or refuses the cloud path. You chose the
  backend per thread; the system respects it.
- **Tools carry a tipping point, expressed as percent saved.** Each efficient
  tool has a heuristic "saves ~N%" vs full `Read`. The point isn't "avoid
  spending" — it's **net efficiency**: a tool that costs tokens but saves more is
  the *right* call. Badges therefore read as **rationale, not alarm**:
  > "used `Outline` — saved ~85% vs full read"
- **Per-answer badge = recommendation with reason**, not a scary meter:
  "index • free" (Tier 0) or "read N files · K turns · ~X% saved vs naive read"
  (agent path). Percent-based so it's intuitive and stable across file sizes.
- The model is **nudged** (via `agents/planner.txt`) to prefer the cheapest tool
  that answers, but is free to use full `Read` when the curated tools don't fit —
  the tipping-point framing makes "spend to save" explicit rather than penalized.

This is a heuristic + transparent-badge model — no real-time token accounting,
just stable per-tool savings hints. (Open-question 3 resolved: suggestion not
enforcement; percent-based.)

---

## Job derivation ("Make jobs from this conversation")

Explicit, **human-gated**, never auto-fired. `POST /api/channel/<cid>/<tid>/derive`.

1. **Derivation agent** (the thread's backend) with a forced JSON contract
   (`agents/planner_derive.txt`): "Propose a MINIMAL set of concrete coding jobs.
   Split independent areas into separate jobs; sequence dependent work as a
   chain. For EACH job, open the real files and include exact `file:line`
   anchors. Return ONLY a JSON array." Schema:
   ```json
   [ {"title":"...","request":"<plain text>","depends_on": <earlier index | null>,
      "anchors":[{"file":"hooks/useFoo.ts","start":30,"end":50}] } ]
   ```
2. **Two-stage anchor verification — confirm before a proposal is shown**
   (open-question 2 resolved: confirm, don't just badge):
   - **Stage A — existence:** for every cited anchor, `planner.py` re-greps/
     re-reads it against the **live repo** and **drops anchors that don't
     resolve** (catches fabricated citations). Anchors are read-pointers ("read
     around here") preferring symbol names + small ranges over exact lines, since
     `base_branch` is frozen and HEAD may shift.
   - **Stage B — relevance confirm:** a second pass **re-Reads each surviving
     anchor's range and asks the planning model to confirm the anchor actually
     supports the job's claim** (one cheap focused check per job, not per
     anchor). Anchors that fail confirmation are dropped; a job left with **no
     confirmed anchors is held back** from the proposal (flagged "needs a human
     anchor") rather than shown as if grounded. This makes baked snippets a
     feature, not a footgun — a proposal only surfaces once its anchors are
     verified to exist *and* to be on-point. Local-derived proposals still carry
     a lower-confidence badge, but they no longer reach you unconfirmed.
3. **Bake into request text** in the review-job style (mirror the
   `submit_review_job` formatter, [job_queue.py:252](../lib/job_queue.py)):
   > "Fix null handling in useFoo. Context to read first: `hooks/useFoo.ts:30-50`.
   > The bug is at `hooks/useFoo.ts:42` where `opts` may be undefined."
4. **Editable proposal** rendered as cards (title + request textarea + anchor
   chips + "depends on ▸" selector). You review/edit before queuing.
5. **Submit (zero new queue logic):** walk jobs in `seq` order calling the
   existing `submit_job(request, repo=channel.repo, priority, after=<resolved
   parent job_id>)` ([job_queue.py:205](../lib/job_queue.py)). The **only** new
   code is a ~20-line `depends_on` index → real `job_id` remap. `depends_on:null`
   → chain roots (multiple independent jobs = multiple roots). Chains,
   `queue_claim` blocking, `_branch_job_id` routing, and `accept_chain` staging
   all work **unchanged**.

**Isolation (decision 2):** the submitted job's `request` is the *only* thing
that carries forward — the anchors are inlined as text. No thread/transcript/
channel field is ever written to the job, so the runner never sees the chat.

---

## UI (inline in `serve.py`'s `HTML_TEMPLATE`)

A top-level **"Queue | Channels"** toggle; clicking Channels swaps the main panel
without disturbing the running queue or worker SSE. Three zones in the Channels
view:

1. **Left rail** — channels (per repo) → expand to **threads**; "+ New thread";
   below the active thread, the **Citations rail** (evidence locker): `file:line`
   + a syntax-tinted snippet reusing the diff palette
   ([serve.py diff styles](../lib/serve.py)); click to scroll the chat to where
   it was cited.
2. **Center** — chat transcript. Each grounded answer wears a **cost badge**
   ("index • free" or "read N files · K turns") and a "grounded in: `file:line`"
   footer (clickable). While an agent question runs, the bubble **streams the
   agent's live Read/Grep calls** (same SSE progressive render as the worker log)
   — you *see* it opening files. Input = textarea + Send + "Dig deeper" toggle.
3. **Header** — per-thread backend selector (local|cloud + model), editable live;
   plus "Index: N symbols · built Xm ago · Rebuild".

**Make-jobs:** a "Make jobs from this conversation" button streams the derivation
agent, then renders proposal **cards** in a right drawer (editable title +
request + anchor chips + depends-on). "Submit all as chain" / per-card Submit.
After submit, cards become linked job chips deep-linking into the queue's job
drawer; a toast confirms ("Queued 3 jobs, chained A→B→C").

---

## Endpoints (`serve.py` → `lib/planner.py` + `lib/channels.py`)

Reuse `_read_body()`, `_send_json()`, and the dirpicker `/api/browse` flow.
**Concurrency note:** the worker SSE buffers are singletons that refuse
concurrent runs — channels need **dict-keyed buffers** (`_ask_log[tid]`,
`_ask_cond[tid]`), a real rewrite (budgeted), so two threads (or a thread + a
running job) can stream at once.

**GET**
- `/api/channels` — list channels (per repo) + their threads.
- `/api/channel/<cid>/<tid>` — thread header + transcript + citations.
- `/api/channels/models` — `{cloud:[...], local:[...]}` (both lists always;
  removes the `is_local()` gate for channels).
- `/api/ask-stream?cid&tid&q&dig` — SSE: runs the grounding flow; index path
  returns immediately; agent path spawns the read-only subprocess in a daemon
  thread, streams tool events + tokens, then the answer. Survives browser close.

**POST**
- `/api/channel/create` — `{repo}`; cid from repo path; detect profile, capture
  base_branch, build the index.
- `/api/channel/<cid>/thread/create` — `{planning_mode, planning_model}`.
- `/api/channel/<cid>/<tid>/derive` — SSE; derivation agent + anchor guard;
  writes a proposal; streams cards.
- `/api/channel/<cid>/<tid>/proposal/<pid>` — save edited proposal.
- `/api/channel/<cid>/<tid>/submit` — `{proposal_id, included_seqs}`; walks jobs
  → `submit_job(after=...)`; records `submitted_job_ids`; appends a linking
  transcript turn; returns `[{seq, job_id, name}]`.
- `/api/channel/<cid>/<tid>/set-model`, `/api/channel/<cid>/reindex`,
  `/api/channel/<cid>/<tid>/delete`.

`/api/settings` SCHEMA gains `planning_max_turns`, `planning_default_mode`,
`planning_default_model`. No new secret surface.

---

## Build plan (files, ordered for independent testing)

1. **`lib/ollama_worker.py`** (~35 lines) — a genuine `TOOLS_PLANNING` read-only
   set: filter `_make_tools` to Read/Grep/Glob/LS (note `write_enabled=False`
   still returns Edit+Bash — [ollama_worker.py:398](../lib/ollama_worker.py)) and
   register the curated efficient tools (`Outline`/`Signature`/`ReadSymbol`/
   `Usages`). Add an `--ask` `__main__` branch that runs `run_agent_loop` with
   `TOOLS_PLANNING` and `planning_max_turns`.
2. **`lib/channels.py`** (NEW, ~180 lines) — `channels_init()`; channel + thread
   header / `.jsonl` / citations read-write (atomic); proposal CRUD; cid from
   repo path; reuse `generate_name`/`_detect_profile`.
3. **`lib/planner.py`** (NEW, ~380 lines) — broadened `build_symbol_map`; the
   regex classifier + Tier-0 index answer; the curated efficient-tool
   implementations (`Outline`/`Signature`/`ReadSymbol`/`Usages` on existing
   primitives, each with a percent-savings hint); agent dispatch for both
   backends (subprocess); citation harvesting; derivation + **two-stage anchor
   verification** (existence re-grep + relevance re-Read confirm); the
   review-style anchor formatter.
4. **`agents/planner.txt`** + **`agents/planner_derive.txt`** (NEW) — read-only
   system prompt + strict-JSON derivation contract.
5. **`lib/settings.py`** (~15 lines) — the three planning knobs.
6. **`lib/serve.py`** (~450 lines, mostly inline HTML) — Queue|Channels toggle,
   three-zone view, proposal drawer, ~10 route branches, dict-keyed per-thread
   SSE buffers, ungate the model-list endpoints.
7. **`CLAUDE.md`** — document `channels/`, per-thread planning backend, the
   read-only-subprocess sandbox, and the **job-isolation rule**.

No installer / queue-format / Docker-mount / `submit_job` changes — `channels/`
is just another state dir under `AGENTIC_HOME`; profiles/agents read from
`AGENTIC_APP` exactly like today. **~750–1050 net lines, dominated by the UI
string.**

---

## Phasing (validate the bet before building it all)

**Slice 1 — the core bet (local, single thread, no derivation):**
the curated `TOOLS_PLANNING` read-only set + `--ask`; minimal `channels.py`
(channel + one thread); `planner.py` with the broadened index + classifier +
Tier-0 + local agent dispatch + the efficient tools with percent-savings hints;
`agents/planner.txt`; a barebones Channels tab (create a channel, one thread,
chat box, `/api/ask-stream` with the cost/savings badge). Single-thread SSE can
temporarily reuse a singleton buffer.
**Validate:** ask *"where is X"* (instant, free) and *"how does X work"* (agent
opens real files via the efficient tools, cites `file:line`). **Does grounded
beat one-shot? Does the index path feel instant? Do the efficient tools visibly
save tokens?** This is the whole bet — prove it before more.

**Slice 2 — make it real:** cloud backend (claude CLI); both-list model picker;
per-thread `set-model`; dict-keyed per-thread SSE (concurrency); multiple threads
per channel + reload.

**Slice 3 — derivation:** `derive` + `planner_derive.txt` + anchor guard;
editable proposal drawer; submit → `submit_job(after=)` remap; deep-link to job
drawer.

**Slice 4 — polish:** citations rail; "Dig deeper"; reindex-on-HEAD-move;
lower-confidence badges on local-derived proposals; "context trimmed" indicator.

---

## Open questions — resolved

All four open questions from the first draft are decided:

1. **Read-in-place vs worktree-per-ask → curated efficient read tools, in place.**
   We *do* want tools (reversing the "no tools" framing), but a **curated
   read-only set** optimized for token efficiency (`Outline`/`Signature`/
   `ReadSymbol`/`Usages` + Read/Grep/Glob/LS), never Edit/Bash. Read-in-place on
   the real working tree is safe because the set is genuinely read-only. See
   [Curated efficient read tools](#curated-efficient-read-tools-tools_planning).

2. **Anchor verification → confirm before proposing.** Two-stage verification
   (existence re-grep + relevance re-Read confirm); a job with no confirmed
   anchors is held back, not shown as grounded. See
   [Job derivation](#job-derivation-make-jobs-from-this-conversation).

3. **Cost signals → suggestion, not enforcement; percent-based.** Local-vs-cloud
   is recommended not auto-enforced; tools express a **tipping point as percent
   saved** ("saved ~85% vs full read") so "spend to save" is explicit. See
   [Cost signals](#cost-signals-suggestion-not-enforcement).

4. **Per-thread cost ceiling → left out.** `planning_max_turns` (8) is the only
   cap; a separate token/cost ceiling is too much for v1.

### Still genuinely open (decide during build, low-stakes)

- Default value for `planning_default_mode` (local vs cloud) on a fresh install.
- Whether the efficient-tool savings hints are static constants or tuned once we
  see real usage (start static).

---

## Why this fits agentic

- Reuses the tool loop, symbol map, model-call paths, and `submit_job` chaining —
  ~80% substrate already exists.
- JSON-on-disk under `AGENTIC_HOME`, no DB, stdlib-only, inline UI — honors every
  architectural constraint.
- The **job-isolation rule** keeps the chat a scoping aid, never a hidden context
  source — so job runs stay reproducible from their `request` alone.

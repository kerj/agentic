# Agentic

Queue jobs for Claude Code to work on while you do something else. Review the diffs, merge what you like.

---

## What it does

You submit a request, Claude Code runs as an agent in an isolated git branch, does the work, verifies the build passes, and commits. You come back whenever, look at what it did, and decide whether to merge.

Nothing touches your working tree until you explicitly merge it.

---

## Setup

**Requirements:** zsh, jq, git, Python 3, Claude Code CLI (`claude`)

```bash
git clone <repo-url> ~/.agentic
bash ~/.agentic/install.sh
source ~/.zshrc
```

---

## Usage

```bash
cd your-project
agentic serve
```

Open [http://localhost:4080](http://localhost:4080). The dashboard is locked to the project you launched from.

**Submit a job** — describe what you want in the text area and hit Submit (or `Cmd+Enter`).

**Run it** — hit **▶ Run Worker** for one job, or **▶▶ Run All** to process the whole queue.

**Review** — when a job finishes, click it to see what the agent did: files read, files modified, commands run, build result. Click **View Diff** to see the code changes.

**Merge** — click **Accept** (single job) or **Accept Chain** (whole sequence) to collect the agent work onto a staging branch like `agent-work/20260520-a1b2`. Then merge that branch into your own work whenever you're ready.

```bash
git merge agent-work/20260520-a1b2
```

---

## Chaining jobs

Jobs can build on each other. When submitting, fill in the **Chain after** field with a previous job's ID. Each chained job branches from the previous job's committed work, so the agent sees what the previous job actually built.

**Accept Chain** on the first job in a sequence merges all of them together onto one staging branch in the correct order. You get one branch to review and merge rather than five.

---

## Server commands

```bash
agentic serve          # start on port 4080
agentic serve 8080     # custom port
agentic serve stop     # stop
agentic serve status   # check if running
```

---

## Review workflow

| Action | What it does |
|---|---|
| **View Diff** | Colour-coded diff of everything the agent changed |
| **Accept** | Merge the job's branch onto the staging branch |
| **Accept Chain ↓** | Merge the whole chain onto a single staging branch |
| **Reject** | Delete the branch and worktree |
| **Abandon** | Move a stuck running job to failed so it can be retried |

After **Accept Chain**, you have an `agent-work/<date>` branch. Merge it into your own branch when you're satisfied with it.

---

## What the agent does

For each job the agent:

1. Reads `CLAUDE.md` to understand project conventions
2. Explores relevant source files
3. Implements the requested change
4. Runs `npm install` if `package.json` was modified
5. Runs `npm run build` — fixes all type errors, missing imports, and bundler errors
6. Runs `npm run lint` if available — fixes ESLint errors including React hook rules
7. Runs `npx prettier --write src/` if Prettier is configured
8. Commits with a descriptive message

The job detail page shows everything the agent did: files read, files modified, every command run with pass/fail status, and the full agent reasoning if you want to see it.

---

## What gets created

```
~/.agentic/
├── queue/
│   ├── pending/      # submitted, waiting to run
│   ├── running/      # claimed by worker, in progress
│   ├── done/         # completed — branch ready to review
│   ├── failed/       # build or agent error
│   ├── abandoned/    # worker crashed or manually stopped
│   └── cancelled/    # cancelled before running
├── worktrees/
│   └── j_xxx/        # isolated checkout per job, removed after accept/reject
└── serve.pid         # server PID, removed on stop
```

In your project, the agent creates branches named `agentic/<job-id>`. Accept Chain creates `agent-work/<date>-<short-id>`. Neither touches your working tree.

---

## CLAUDE.md

The agent reads `CLAUDE.md` at the root of your project before doing anything. Keep it current — it's the primary source of truth for naming conventions, import patterns, testing framework, component patterns, and anything else the agent needs to know.

```bash
agentic doc-gen   # generate from project analysis
agentic doc       # open in $EDITOR
```

---

## Other commands

```bash
agentic init          # initialize project (creates CLAUDE.md, updates .gitignore)
agentic accept <id>   # merge a single job's branch
agentic reject <id>   # discard a job's branch and worktree
agentic plan          # create an agile plan from a request
```

---

MIT License

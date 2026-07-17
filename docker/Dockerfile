# agentic — all-in-one container (UI + queue + worker spawner)
#
# v1 scope: LOCAL mode, TypeScript projects. Ollama runs NATIVELY on the host
# (GPU/models live there) and is reached at host.docker.internal:11434 — there is
# NO ollama in this image (it runs on the host). The claude CLI IS installed, so
# cloud execution + cloud planning work. The container runs serve.py, which
# serves the dashboard and spawns `agentic worker-once` per job.
#
# The app source is baked at /opt/agentic. Per-host STATE (queue, worktrees,
# diffs, logs, settings.json, secrets.json) lives on a bind-mounted volume at
# runtime — see docker-compose.yml. Nothing secret is copied into the image
# (.dockerignore enforces this).

# No Node is baked into the base — fnm owns ALL Node versions so different
# projects can pin different versions (.nvmrc / .node-version / engines.node) and
# you're never locked to one baked Node. A default LTS is installed below.
FROM debian:bookworm-slim

# Toolchain: git + jq for the bash queue/worktree plumbing, python3 + venv for
# the Python modules, curl/unzip to fetch fnm, ca-certificates + gosu so the
# entrypoint can drop to the host UID:GID.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        jq \
        python3 \
        python3-venv \
        ca-certificates \
        curl \
        unzip \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# ── fnm (Fast Node Manager) — system-wide, scriptable, multi-version ─────────
# fnm is a single binary (no shell-function sourcing like nvm), so the worker can
# invoke it non-interactively. Install it and a DEFAULT LTS Node to a SYSTEM dir
# readable by any uid (the entrypoint drops to HOST_UID at runtime).
ENV FNM_DIR=/usr/local/share/fnm
ENV PATH=/usr/local/bin:$PATH
# Node 22 (current LTS). Must be >= 22.13: modern pnpm (11.x) requires it and uses
# node:sqlite, which older Node lacks. Per-project versions still switch via fnm at
# run time (.nvmrc / .node-version / engines.node); this is only the default.
ENV DEFAULT_NODE=22
# corepack must never block on an interactive "download pnpm@X? (Y/n)" prompt —
# the worker runs unattended. Auto-approve pinned-version downloads.
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN curl -fsSL https://fnm.vercel.app/install | bash -s -- --install-dir /usr/local/bin --skip-shell \
    && fnm install "${DEFAULT_NODE}" \
    && fnm default "${DEFAULT_NODE}" \
    # Symlink the default version's node/npm/npx into /usr/local/bin so scripts
    # and the worker find them on PATH with NO shell eval. Per-project switches
    # still go through `fnm use`/`fnm exec` (wired in the worker).
    && DEFAULT_BIN="$(dirname "$(fnm exec --using=${DEFAULT_NODE} which node)")" \
    && ln -sf "$DEFAULT_BIN/node"     /usr/local/bin/node \
    && ln -sf "$DEFAULT_BIN/npm"      /usr/local/bin/npm \
    && ln -sf "$DEFAULT_BIN/npx"      /usr/local/bin/npx \
    && ln -sf "$DEFAULT_BIN/corepack" /usr/local/bin/corepack \
    # ── corepack: pnpm + yarn on PATH, at the version each project pins ────────
    # corepack ships with Node and is the official way to provide pnpm/yarn at the
    # version a project pins via its package.json `packageManager` field. Without
    # this, `pnpm install` is "command not found" and the worker burns hours
    # inventing workarounds. corepack itself must be on PATH first (symlinked
    # above) — both for THIS `enable` step and for the worker's runtime
    # `corepack prepare`. Install the shims into /usr/local/bin (node-agnostic:
    # they just need SOME node on PATH), so pnpm/pnpx/yarn resolve for ANY uid and
    # survive per-project fnm Node switches. The pinned pm VERSION is downloaded
    # lazily at run time into COREPACK_HOME (a persistent state-mount dir).
    && corepack enable --install-directory /usr/local/bin \
    && chmod -R a+rX "$FNM_DIR" \
    && chmod a+rx /usr/local/bin/pnpm /usr/local/bin/pnpx /usr/local/bin/yarn 2>/dev/null || true \
    # Verify the shims are INSTALLED (on PATH) without EXECUTING them: running
    # `pnpm --version` would make corepack download the latest pnpm into the image
    # at build time — wasteful (real downloads happen at run time into the
    # persistent COREPACK_HOME) and it fails if that pnpm outpaces DEFAULT_NODE.
    # The per-project pnpm/yarn VERSION is resolved lazily at run time.
    && node --version && npm --version \
    && command -v pnpm >/dev/null && command -v yarn >/dev/null \
    && echo "corepack shims installed: $(command -v pnpm), $(command -v yarn)"

# ── Claude Code CLI — enables CLOUD execution + CLOUD planning ───────────────
# The cloud worker (worker.sh) and the cloud planning agent (planner.py) both
# shell out to `claude`. Install it globally on the baked Node and symlink onto
# PATH so the dropped-uid worker finds it. Auth is the ANTHROPIC_API_KEY env var,
# injected per-process from secrets.json by serve.py / planner.py (never baked).
RUN npm install -g @anthropic-ai/claude-code \
    # $DEFAULT_BIN from an earlier RUN doesn't persist across layers. The global
    # bin dir is <npm prefix>/bin; symlink the installed `claude` onto PATH.
    && GLOBAL_BIN="$(npm config get prefix)/bin" \
    && ln -sf "$GLOBAL_BIN/claude" /usr/local/bin/claude \
    && chmod -R a+rX "$FNM_DIR" \
    && /usr/local/bin/claude --version

# Build the app's own venv. worker.sh and bin/agentic invoke
# APP SOURCE vs STATE split: the source (incl. venv) lives here at AGENTIC_APP
# and is NEVER mounted. The code resolves source/venv via AGENTIC_APP/
# AGENTIC_PYTHON, so the container never writes source into the state mount —
# no symlink hack, no rm. (The old entrypoint rm -rf'd source into the mount;
# that whole mechanism is gone.)
ENV AGENTIC_APP=/opt/agentic
ENV AGENTIC_PYTHON=/opt/agentic/venv/bin/python3
RUN python3 -m venv ${AGENTIC_APP}/venv
# The app is stdlib-only (no requirements.txt) — the venv exists purely to give
# worker.sh/bin/agentic a stable python3 at the expected path. If a
# requirements file is ever added, pip install it here.

WORKDIR ${AGENTIC_APP}

# Copy only tracked source. .dockerignore blocks secrets.json / settings.json /
# serve.pid / queue / worktrees / diffs / logs / venv from ever entering a layer.
COPY bin/      ${AGENTIC_APP}/bin/
COPY lib/      ${AGENTIC_APP}/lib/
COPY agents/   ${AGENTIC_APP}/agents/
COPY profiles/ ${AGENTIC_APP}/profiles/
COPY CLAUDE.md README.md ${AGENTIC_APP}/

RUN chmod +x ${AGENTIC_APP}/bin/agentic ${AGENTIC_APP}/bin/agentic-switch 2>/dev/null || true

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# SAFETY GUARD: the entrypoint's contract is that it never destroys host data.
# Fail the build if a destructive command (rm / mv / recursive chown / symlink)
# appears in ACTUAL CODE — a tripwire so the prior source-loss incident can't
# silently return via a future edit. Comment lines (which legitimately *describe*
# the ban) are stripped first so they don't false-positive.
RUN sed 's/#.*$//' /usr/local/bin/entrypoint.sh \
      | grep -nE '(^|[^[:alnum:]_])(rm|mv)[[:space:]]|chown[[:space:]]+-R|ln[[:space:]]+-s' \
      && { echo "BUILD FAILED: entrypoint.sh contains a destructive command (rm/mv/chown -R/ln -s)." >&2; exit 1; } \
      || echo "entrypoint safety check passed (no destructive commands)"

EXPOSE 4080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

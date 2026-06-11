# agentic — all-in-one container (UI + queue + worker spawner)
#
# v1 scope: LOCAL mode, TypeScript projects. Ollama runs NATIVELY on the host
# (GPU/models live there) and is reached at host.docker.internal:11434 — there is
# NO ollama and NO claude CLI in this image. The container runs serve.py, which
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
ENV DEFAULT_NODE=20
RUN curl -fsSL https://fnm.vercel.app/install | bash -s -- --install-dir /usr/local/bin --skip-shell \
    && fnm install "${DEFAULT_NODE}" \
    && fnm default "${DEFAULT_NODE}" \
    # Symlink the default version's node/npm/npx into /usr/local/bin so scripts
    # and the worker find them on PATH with NO shell eval. Per-project switches
    # still go through `fnm use`/`fnm exec` (wired in the worker).
    && DEFAULT_BIN="$(dirname "$(fnm exec --using=${DEFAULT_NODE} which node)")" \
    && ln -sf "$DEFAULT_BIN/node" /usr/local/bin/node \
    && ln -sf "$DEFAULT_BIN/npm"  /usr/local/bin/npm \
    && ln -sf "$DEFAULT_BIN/npx"  /usr/local/bin/npx \
    && chmod -R a+rX "$FNM_DIR" \
    && node --version && npm --version

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

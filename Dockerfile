# Alchemist runtime image.
#
# Bundles every CLI alchemist orchestrates so the container is self-contained
# under Railway cron. Composition is by file/CLI contract (Doctrine 0001/0003/
# 0004) — alchemist Python code never imports conductor or touchstone, only
# shells out to them.
#
# Entry: `alchemist run-once` (cron-friendly: runs once and exits).
#
# Build:
#   docker build -t alchemist:dev .
# Smoke:
#   docker run --rm -e GITHUB_TOKEN=$(gh auth token) alchemist:dev alchemist doctor

FROM python:3.12-slim AS base

ARG TOUCHSTONE_VERSION=v2.11.38
ARG CONDUCTOR_VERSION=v0.10.26
ARG UV_VERSION=0.11.13
# hatch-vcs reads the version from git history; the build context excludes
# .git so we pin a pretend-version. CI release builds override via --build-arg
# so the published image matches the tag.
ARG ALCHEMIST_VERSION=0.0.1.dev0

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIPX_HOME=/opt/pipx \
    PIPX_BIN_DIR=/usr/local/bin \
    TOUCHSTONE_ROOT=/opt/touchstone \
    PATH=/opt/touchstone/bin:/usr/local/bin:$PATH

# Base system packages: gh CLI, git, jq (for branch-guard hooks if any),
# bash (touchstone is bash). curl is bundled by debian:slim.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        jq \
        bash \
        figlet \
 && mkdir -p -m 755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

# pipx for isolated Python tool installs (conductor + alchemist itself).
# uv is needed because many target-repo touchstone preflight validate steps
# run via `uv run ...`.
RUN pip install --no-cache-dir pipx==1.7.1 uv==${UV_VERSION}

# Touchstone: cloned at a pinned tag, exposed at $TOUCHSTONE_ROOT and on PATH.
# Alchemist invokes $TOUCHSTONE_ROOT/scripts/codex-review.sh from the cloned
# target-repo's root.
RUN git clone --depth 50 --branch ${TOUCHSTONE_VERSION} \
        https://github.com/autumngarage/touchstone.git ${TOUCHSTONE_ROOT} \
 && chmod +x ${TOUCHSTONE_ROOT}/bin/touchstone

# Conductor: cloned from source at a pinned tag (it isn't published on PyPI
# under the name `conductor`), then pipx-installed from the local checkout.
# Mirrors the brew formula's install path.
RUN git clone --depth 50 --branch ${CONDUCTOR_VERSION} \
        https://github.com/autumngarage/conductor.git /opt/conductor \
 && pipx install /opt/conductor

# Alchemist itself.
WORKDIR /opt/alchemist
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${ALCHEMIST_VERSION}
RUN pipx install . \
 && chmod +x scripts/railway-entrypoint.sh \
 && rm -rf /root/.cache/pip /root/.cache/uv

# Persistent state lives outside the image; Railway mounts a volume here.
RUN mkdir -p /var/alchemist/state

# Default to a single tick. Auth (App installation token vs PAT) is resolved
# internally by alchemist's CLI, so startup must not require GITHUB_TOKEN.
CMD ["bash", "scripts/railway-entrypoint.sh"]

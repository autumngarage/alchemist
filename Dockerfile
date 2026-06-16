# Alchemist runtime image.
#
# Bundles the GitHub CLI Alchemist needs to coordinate issue/PR state under
# Railway cron. External coding agents run in Codex/Devin, not in this image.
#
# Entry: `alchemist run-once` (cron-friendly: runs once and exits).
#
# Build:
#   docker build -t alchemist:dev .
# Smoke:
#   docker run --rm -e GITHUB_TOKEN=$(gh auth token) alchemist:dev alchemist doctor

FROM python:3.12-slim AS base

# hatch-vcs reads the version from git history; the build context excludes
# .git so we pin a pretend-version. CI release builds override via --build-arg
# so the published image matches the tag.
ARG ALCHEMIST_VERSION=0.0.1.dev0

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIPX_HOME=/opt/pipx \
    PIPX_BIN_DIR=/usr/local/bin \
    PATH=/usr/local/bin:$PATH

# Base system packages: gh CLI plus bash for the entrypoint.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
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

# pipx for isolated Alchemist installation.
RUN pip install --no-cache-dir pipx==1.7.1

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

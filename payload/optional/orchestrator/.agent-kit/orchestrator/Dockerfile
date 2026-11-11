# Agent orchestrator image — boots, scans the tickets dir, launches headless opencode
# agent sessions (agentctl.sh) that open PRs, retries failures, plans waves via an
# orchestrator session. Repo is cloned at boot into the /workspace volume, never baked in.
#
# Build:  make agents-image
# Run:    make agents-orchestrator   (env: .env.agents — GH_TOKEN + REPO_SLUG required)
FROM node:22-bookworm

ARG OPENCODE_VERSION=1.18.33
ARG GH_CLI_VERSION=2.62.0
# Pin to the pnpm major your lockfile was written with (an unpinned `pnpm@latest` can rewrite it).
# Using npm/yarn instead? Drop the corepack line below. Other stacks (python, go…): add their
# toolchain to this file — it is yours to edit.
ARG PNPM_VERSION=10.34.6

RUN apt-get update && apt-get install -y --no-install-recommends \
      git \
      make \
      python3 \
      perl \
      procps \
      curl \
      ca-certificates \
      openssh-client \
      gnupg \
      jq \
      ripgrep \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (agent-finish.sh opens PRs with `gh`)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# pnpm via corepack
RUN corepack enable && corepack prepare "pnpm@${PNPM_VERSION}" --activate

# opencode, pinned to the version the host runs
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}" && npm cache clean --force

# Session/log/pid state lives under /workspace (volume) and is written by root.
ENV HOME=/root \
    SHELL=/bin/bash \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0

WORKDIR /workspace

# The orchestrator scripts are copied outside /workspace: a missing or empty workspace volume
# can therefore never shadow the entrypoint, and boot falls back to these copies when the
# cloned repo does not carry .agent-kit/orchestrator (e.g. first boot on an older main).
COPY boot.sh next-tickets.py state.py supervisor.sh planner-prompt.md /boot/
RUN chmod +x /boot/boot.sh /boot/supervisor.sh /boot/next-tickets.py /boot/state.py

ENTRYPOINT ["/boot/boot.sh"]

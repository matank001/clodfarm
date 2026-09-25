# clodfarm: always-on Claude Code agents. Runs as the non-root user "farm";
# the container itself is the sandbox the agents work in.
FROM debian:bookworm-slim
LABEL org.opencontainers.image.title="clodfarm" \
      org.opencontainers.image.description="Always-on Claude Code agents with Remote Control, sub-agents and a per-account budget governor" \
      org.opencontainers.image.source="https://github.com/matank001/clodfarm" \
      org.opencontainers.image.licenses="MIT"

ARG CLAUDE_VERSION=stable
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PATH=/home/farm/.local/bin:/opt/clodfarm/bin:$PATH \
    CLAUDE_CONFIG_DIR=/home/farm/.claude \
    DISABLE_AUTOUPDATER=1 \
    FARM_WORKSPACE=/workspace \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git openssh-client tmux jq less procps ripgrep tini \
      python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 -s /bin/bash farm \
    && mkdir -p /workspace /home/farm/.claude && chown farm:farm /workspace /home/farm/.claude

COPY --chown=farm:farm pyproject.toml README.md /src/
COPY --chown=farm:farm clodfarm /src/clodfarm
RUN python3 -m venv /opt/clodfarm && /opt/clodfarm/bin/pip install --no-cache-dir /src && rm -rf /src

USER farm
WORKDIR /workspace
# Claude Code native build (https://docs.claude.com/en/docs/claude-code/setup)
RUN curl -fsSL https://claude.ai/install.sh | bash -s -- "$CLAUDE_VERSION" && claude --version \
    && git config --global user.name "clodfarm" && git config --global user.email "clodfarm@localhost" \
    && git config --global init.defaultBranch main

VOLUME ["/home/farm/.claude", "/workspace"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["clodfarm", "run"]

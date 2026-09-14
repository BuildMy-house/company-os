FROM nousresearch/hermes-agent:latest

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV PATH="/opt/company-ops-venv/bin:${PATH}"
WORKDIR /opt/company-ops

RUN apt-get update \
  && apt-get install -y --no-install-recommends python3-venv git openssh-client curl postgresql-client \
  && rm -rf /var/lib/apt/lists/* \
  && mkdir -p /root/.ssh \
  && ssh-keyscan github.com >> /root/.ssh/known_hosts 2>/dev/null

# opencode intentionally NOT installed here — Hermes never invokes opencode
# directly (721MB saved); coding work is dispatched to the `engineering`
# container via the engineering_manager MCP, whose ai-cli-mcp router is the
# only place opencode/claude/codex actually run. Browser automation (via
# hermes-agent's bundled Playwright/Chromium below) stays — Hermes uses it
# to interact with buildmyhouse directly.

# Infisical CLI isn't published to npm as "infisical" — install the real
# binary via the official apt repo instead.
RUN curl -1sLf 'https://artifacts-cli.infisical.com/setup.deb.sh' | bash \
  && apt-get update && apt-get install -y --no-install-recommends infisical \
  && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-browser --skip-computer-use --skip-setup || true \
  && mkdir -p /root/.hermes /opt/data \
  && command -v hermes

COPY . /opt/company-ops/
COPY hermes/config.yaml /opt/data/config.yaml
COPY hermes/SOUL.md /root/.hermes/SOUL.md
RUN python3 -m venv /opt/company-ops-venv \
  && /opt/company-ops-venv/bin/pip install --no-cache-dir -e '/opt/company-ops[backup]'

ENTRYPOINT ["bash", "/opt/company-ops/scripts/entrypoint.sh"]
CMD ["hermes"]

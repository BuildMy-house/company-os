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
# engineering team owns source work and repository access. Hermes only gets a
# narrowly-scoped BuildKit image-build tool. Browser automation (via
# hermes-agent's bundled Playwright/Chromium below) stays — Hermes uses it
# to interact with buildmyhouse directly.

# Infisical CLI isn't published to npm as "infisical" — install the real
# binary via the official apt repo instead.
RUN curl -1sLf 'https://artifacts-cli.infisical.com/setup.deb.sh' | bash \
  && apt-get update && apt-get install -y --no-install-recommends infisical \
  && rm -rf /var/lib/apt/lists/*

# kubectl: the dns-healthcheck CronJob (company_ops/dns_healthcheck.py)
# shells out to `kubectl logs -n kube-system -l k8s-app=kube-dns`, and the
# base image ships no kubectl — without this the Job would fail every 15m on
# FileNotFoundError. Static binary from the official release channel;
# amd64 because this repo builds/deploys on x86_64 hosts.
RUN curl -fsSL -o /tmp/kubectl "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
  && install -m 0755 /tmp/kubectl /usr/local/bin/kubectl \
  && rm -f /tmp/kubectl \
  && kubectl version --client

RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-computer-use --skip-setup || true \
  && mkdir -p /root/.hermes /opt/data \
  && command -v hermes

COPY . /opt/company-ops/
RUN chmod 755 /opt/company-ops/scripts/hive-member-mcp.js
COPY hermes/config.yaml /opt/data/config.yaml
COPY hermes/SOUL.md /root/.hermes/SOUL.md
COPY hermes-plugins/axiom_usage /opt/hermes/plugins/observability/axiom_usage
COPY hermes-plugins/model_retirement_watch /opt/hermes/plugins/observability/model_retirement_watch
RUN python3 -m venv /opt/company-ops-venv \
  && /opt/company-ops-venv/bin/pip install --no-cache-dir -e '/opt/company-ops[backup]'

ENTRYPOINT ["bash", "/opt/company-ops/scripts/entrypoint.sh"]
CMD ["hermes"]

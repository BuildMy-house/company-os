# Base pinned by digest (resolved read-only from Docker Hub 2026-10-13, was :latest).
# Bump deliberately: look up the new digest, update here, rebuild, re-pin.
FROM nousresearch/hermes-agent@sha256:444b42594e7f467425872fc35e077a227d377f0fe0726bc818706d62175fad8b

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

# Hindsight memory provider (Hermes plugin `hindsight`; Hermes memory.provider
# selects it in hermes/config.yaml). Baked at build time, never via
# `hermes plugins install` at runtime: that clones from GitHub into
# $HERMES_HOME/plugins, i.e. the /opt/data PVC, and would need egress + a git
# clone on every fresh volume.
#  - Plugin source is the exact commit pinned in this Hermes release's reviewed
#    plugin catalog (plugin-catalog/hindsight.yaml, v1.2.1); sparse-fetched so
#    the 170 MB monorepo is not downloaded. Bump together with the base digest.
#  - Only hindsight-client is installed (local_external mode talks HTTP to the
#    hindsight Deployment). hindsight-embed (the plugin's other declared dep) is
#    only imported for local_embedded, which refuses to run as root and would
#    put ~1 GB of embedding models inside the 2Gi hermes pod, so it is omitted.
#    Pinned to 0.10.3: the plugin requires hindsight-client>=0.10.1,<1 and
#    0.10.3 is the version of the hindsight server image in k8s/base/hindsight.
#  - The hermes venv ships without pip; bootstrap it, install, then remove it.
ARG HINDSIGHT_PLUGIN_SHA=d56c4acdf59c41957613d399094cdf8c489b060c
RUN set -eux; \
  PY=/opt/hermes/.venv/bin/python; \
  $PY -m ensurepip >/dev/null; \
  $PY -m pip install --no-cache-dir "hindsight-client==0.10.3"; \
  $PY -m pip uninstall -y pip; \
  d=/tmp/hindsight-src; rm -rf "$d"; mkdir "$d"; cd "$d"; \
  git init -q; \
  git remote add origin https://github.com/vectorize-io/hindsight; \
  git config core.sparseCheckout true; \
  echo "/hindsight-integrations/hermes/" > .git/info/sparse-checkout; \
  git fetch -q --depth 1 --filter=blob:none origin "$HINDSIGHT_PLUGIN_SHA"; \
  git checkout -q FETCH_HEAD; \
  test "$(git rev-parse HEAD)" = "$HINDSIGHT_PLUGIN_SHA"; \
  dest=/opt/hermes/plugins/memory/hindsight; rm -rf "$dest"; mkdir -p "$dest"; \
  cp hindsight-integrations/hermes/*.py hindsight-integrations/hermes/plugin.yaml "$dest"/; \
  rm -rf "$d"; \
  PYTHONPATH=/opt/hermes $PY -c "import hindsight_client, plugins.memory.hindsight"

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

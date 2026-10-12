# pm-agent: Hermes-runtime product-feedback agent (see pm-agent/SOUL.md and
# docs/PM-AGENT-HERMES.md). Built from the repository root, via the isolated
# BuildKit builder at an immutable source SHA (never from a dirty checkout):
#   docker buildx build -f pm-agent/Dockerfile -t localhost:30500/company-os-pm-agent:<sha> .
FROM nousresearch/hermes-agent:latest

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/pm-agent-venv/bin:${PATH}"

WORKDIR /opt/company-ops

# Deliberately minimal: no git, ssh, kubectl, infisical CLI, node, or
# postgres client. Only the Python the front door and tool server need.
RUN apt-get update \
  && apt-get install -y --no-install-recommends python3-venv \
  && rm -rf /var/lib/apt/lists/* \
  && python3 -m venv /opt/pm-agent-venv

COPY pm-agent/requirements.txt /tmp/requirements.txt
RUN /opt/pm-agent-venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

COPY pm-agent/ pm-agent/
COPY company_ops/ company_ops/
# company_ops/ledger.py reads sql/company_schema.sql at import time (pulled in
# via company_ops/__init__.py), so the container crashes without it.
COPY sql/company_schema.sql sql/company_schema.sql

# Fail the build, not the rollout, if the Hermes runtime is not on PATH.
RUN command -v hermes \
  && chmod 755 pm-agent/entrypoint.sh \
  && useradd --uid 10001 --create-home --home-dir /home/pmagent pmagent \
  && mkdir -p /run/pm-agent && chown pmagent /run/pm-agent

ENV PYTHONPATH=/opt/company-ops:/opt/company-ops/pm-agent

USER 10001
EXPOSE 4200

ENTRYPOINT ["bash", "/opt/company-ops/pm-agent/entrypoint.sh"]

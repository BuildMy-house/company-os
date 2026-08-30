FROM nousresearch/hermes-agent:latest

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV PATH="/opt/company-ops-venv/bin:${PATH}"
WORKDIR /opt/company-ops

RUN apt-get update \
  && apt-get install -y --no-install-recommends python3-venv git openssh-client \
  && rm -rf /var/lib/apt/lists/* \
  && mkdir -p /root/.ssh \
  && ssh-keyscan github.com >> /root/.ssh/known_hosts 2>/dev/null

RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-browser --skip-computer-use \
  && mkdir -p /root/.hermes \
  && command -v hermes

COPY . /opt/company-ops/
COPY hermes/config.yaml /root/.hermes/config.yaml
COPY hermes/SOUL.md /root/.hermes/SOUL.md
RUN python3 -m venv /opt/company-ops-venv \
  && /opt/company-ops-venv/bin/pip install --no-cache-dir -e /opt/company-ops \
  && /opt/company-ops-venv/bin/pip install --no-cache-dir graphifyy

ENTRYPOINT ["bash", "/opt/company-ops/scripts/entrypoint.sh"]
CMD ["hermes"]

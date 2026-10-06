# pm-agent: narrow product-feedback chat service (see pm-agent/SOUL.md).
# Build from the repository root (tested command):
#   docker build -f pm-agent/Dockerfile -t localhost:30500/company-os-pm-agent:latest .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/company-ops

WORKDIR /opt/company-ops

COPY pm-agent/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY pm-agent/ pm-agent/
COPY company_ops/ company_ops/
# company_ops/ledger.py reads sql/company_schema.sql at import time
# (pulled in transitively via company_ops/__init__.py -> Ledger, even
# though pm-agent never uses Ledger itself) -- without this the
# container crashes on startup with FileNotFoundError.
COPY sql/company_schema.sql sql/company_schema.sql

EXPOSE 4200

CMD ["python", "pm-agent/agent.py"]

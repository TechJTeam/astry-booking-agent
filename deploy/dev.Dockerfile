# Astry Booking Agent — FastAPI (vanna) service.
# Built from the repo root via `docker build -f deploy/dev.Dockerfile .` (same convention as
# astry-pos-be/deploy/dev.Dockerfile — buildContext = repo root, dockerfile lives under deploy/).

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV AGENT_SERVER_PORT=8200

EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -f http://127.0.0.1:8200/health || exit 1

CMD ["sh", "-c", "uvicorn src.server:app --host 0.0.0.0 --port ${AGENT_SERVER_PORT:-8200}"]

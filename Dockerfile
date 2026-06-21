# syntax=docker/dockerfile:1.6
# ---- Build stage: install Python deps + warm the embedding model cache ----
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# OS deps for building wheels (kept minimal). sentence-transformers needs libgomp.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Pre-download the embedding model so first run isn't blocked on HF Hub
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# ---- Runtime stage: slim image, no compilers ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONPATH=/app \
    HF_HUB_OFFLINE=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r restartos && useradd -r -g restartos restartos

# Bring deps + model cache from build stage
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
COPY --from=build /root/.cache /home/restartos/.cache

# Application code
COPY restartos/ ./restartos/
COPY config/ ./config/
COPY ui/ ./ui/
COPY dataset/ ./dataset/
COPY _data/ ./_data/
COPY pyproject.toml ./

# Writable state dirs
RUN mkdir -p /app/_it_state && chown -R restartos:restartos /app /home/restartos

USER restartos

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/api/memory > /dev/null || exit 1

CMD ["python", "-m", "restartos.server", "--host", "0.0.0.0", "--port", "8000"]

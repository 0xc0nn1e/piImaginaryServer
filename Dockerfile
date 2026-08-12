# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime-base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 audio-server \
    && useradd --system --uid 10001 --gid audio-server --home-dir /app audio-server

WORKDIR /app


FROM runtime-base AS core-builder

RUN python -m venv /opt/venv \
    && pip install --no-cache-dir --upgrade pip setuptools wheel

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir .


FROM core-builder AS ai-builder

# Install CPU-only PyTorch wheels before the AI extra so pyannote cannot pull a
# CUDA-enabled PyPI wheel into the default worker image.
ARG PYTORCH_VERSION=2.11.0
ARG TORCHCODEC_VERSION=0.13.0
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch==${PYTORCH_VERSION}" "torchaudio==${PYTORCH_VERSION}" \
        "torchcodec==${TORCHCODEC_VERSION}" \
    && pip install --no-cache-dir '.[ai]'


FROM runtime-base AS api

COPY --from=core-builder /opt/venv /opt/venv
COPY --chown=audio-server:audio-server src/ ./src/
COPY --chown=audio-server:audio-server migrations/ ./migrations/
COPY --chown=audio-server:audio-server alembic.ini ./

RUN mkdir -p \
        /data/recordings /data/work /data/staging \
        /models/huggingface /models/torch \
    && chown -R audio-server:audio-server /app /data /models

ENV STORAGE_PATH=/data \
    HF_HOME=/models/huggingface \
    TORCH_HOME=/models/torch

USER audio-server
EXPOSE 8000

CMD ["uvicorn", "audio_server.main:app", "--host", "0.0.0.0", "--port", "8000"]


FROM api AS worker

COPY --from=ai-builder /opt/venv /opt/venv

ENV PYANNOTE_METRICS_ENABLED=0

CMD ["python", "-m", "audio_server.jobs.worker"]


FROM node:22-alpine AS web-builder

WORKDIR /web

COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build


FROM nginxinc/nginx-unprivileged:1.30.4-alpine AS web

COPY --chown=nginx:nginx web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=web-builder --chown=nginx:nginx /web/dist/ /usr/share/nginx/html/

EXPOSE 8080

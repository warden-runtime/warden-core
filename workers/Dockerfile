FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project --extra worker

ENV PATH="/app/.venv/bin:${PATH}"

COPY ./common ./common
COPY ./workers ./workers
COPY ./config/prompts /app/prompts

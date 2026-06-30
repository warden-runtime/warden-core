FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project --extra engine

# CLI entrypoints (watchfiles, uvicorn, etc.) live in the venv; compose uses `watchfiles` / `python`.
ENV PATH="/app/.venv/bin:${PATH}"

COPY ./common ./common
COPY ./engine ./engine
COPY ./config/prompts /app/prompts

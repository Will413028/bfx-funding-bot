FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.5.*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY configs/ ./configs/
COPY scripts/ ./scripts/

RUN uv sync --frozen --no-dev

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# BFX_CELLS_YAML — explicit image-baked default. D1 fix in 4.2.0 also
# adds cwd / importlib fallback so this env is no longer strictly
# required, but kept for explicit deploy-time lookup precedence.
ENV PATH="/app/.venv/bin:$PATH" \
    BFX_CELLS_YAML=/app/configs/cells.yaml

CMD ["sh", "-c", "alembic upgrade head && exec bfx-shadow"]

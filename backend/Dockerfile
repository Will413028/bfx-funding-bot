# syntax=docker/dockerfile:1
# The operator UI (9d-5): the frontend's static export, built from the named build
# context `frontend` (release.yml: build-contexts: frontend=frontend) and served by the
# web API from /app/static. corepack installs the pnpm package.json pins.
FROM node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS ui
RUN corepack enable
WORKDIR /ui
COPY --from=frontend package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY --from=frontend . .
# NEXT_PUBLIC_* are baked into the bundle at build time; the DSN is optional (no Sentry).
ARG NEXT_PUBLIC_SENTRY_DSN
ARG NEXT_PUBLIC_SENTRY_RELEASE
ENV NEXT_PUBLIC_SENTRY_DSN=$NEXT_PUBLIC_SENTRY_DSN \
    NEXT_PUBLIC_SENTRY_RELEASE=$NEXT_PUBLIC_SENTRY_RELEASE \
    NEXT_TELEMETRY_DISABLED=1
RUN pnpm build && test -f out/en/overview/index.html

# The builder: the compiler and uv exist only here. The runtime stage below copies the
# finished virtualenv and the code, so the production image carries neither
# (ASVS V15.2.3). Both stages use the same base, so the venv's interpreter path
# (/usr/local/bin/python3.13) exists in the runtime too.
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.12.23  # same uv as CI (setup-uv) and the VM host

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The project installs editable: the venv points at /app/src, which the runtime stage
# copies to the same path.
COPY src/ ./src/
RUN uv sync --frozen --no-dev

FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=build /app/.venv/ ./.venv/
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY configs/ ./configs/
COPY scripts/ ./scripts/
COPY --from=ui /ui/out/ ./static/

RUN useradd -m -u 1000 appuser && chmod -R go-w /app /usr/local
USER appuser

# GIT_SHA → BFX_SERVICE_VERSION: build arg surfaces the deployed commit as
# the EventResource service.version envelope field. Declared here (late) so a
# per-commit SHA change doesn't invalidate the cached layers above.
# .github/workflows/release.yml passes GIT_SHA=<the CI run's head_sha>.
ARG GIT_SHA=unknown

# BFX_CELLS_YAML — explicit image-baked default. D1 fix in 4.2.0 also
# adds cwd / importlib fallback so this env is no longer strictly
# required, but kept for explicit deploy-time lookup precedence.
ENV PATH="/app/.venv/bin:$PATH" \
    BFX_CELLS_YAML=/app/configs/cells.yaml \
    BFX_SERVICE_VERSION=${GIT_SHA} \
    BFX_WEBAPI_STATIC_DIR=/app/static

# Schema migration is an explicit owner-only operation, never runtime startup.
CMD ["/app/.venv/bin/python", "-m", "bfx_funding_bot.apps.bot"]

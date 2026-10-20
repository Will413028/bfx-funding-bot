# bfx-funding-bot (Python backend)

Python rewrite of the Go backend. Architecture and current rules: [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Setup

```bash
uv sync
cp .env.example .env
# Edit .env: set DATABASE_URL from project-root .env (asyncpg variant: postgresql+asyncpg://...)
```

## Run

```bash
BFX_OPERATOR_TAILNET_USER_ID=operator-1 BFX_WEBAPI_ALLOWED_HOSTS=localhost \
BFX_FE_SENTRY_DSN=https://key@o1.ingest.sentry.io/1 \
uv run uvicorn bfx_funding_bot.apps.webapi:app --host 127.0.0.1 --reload
```

The web API refuses to start without the three ingress values (`IngressSettings`; the
values above are fakes). There is no Tailscale Serve locally: a private route needs the
header Serve would add, e.g.
`-H 'Tailscale-App-Capabilities: {"github.com/Will413028/bfx-funding-bot/cap/operator":[{"user_id":"operator-1"}]}'`;
it only binds 127.0.0.1, so a forged header locally is no risk. Set
`BFX_WEBAPI_STATIC_DIR=../frontend/out` (after `pnpm build` there) to serve the UI too.

## Test

```bash
uv run pytest                    # unit tests (sqlite in-memory)
uv run pytest -m integration     # integration tests (real services; requires network)
uv run mypy src/                 # type check
uv run ruff check                # lint
uv run ruff format --check       # format check
```

## Migrations

```bash
uv run alembic current
uv run alembic revision --autogenerate -m "<change>"
uv run alembic upgrade head
```

## Layout

Vertical-sliced. See plan doc for module composition rules.

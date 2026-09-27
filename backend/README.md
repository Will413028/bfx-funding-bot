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
uv run uvicorn bfx_funding_bot.main:app --reload
```

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

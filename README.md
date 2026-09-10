# Deep Research Agent

Telegram-accessible Deep Research Agent. See [`docs/PLAN.md`](docs/PLAN.md) as the source of truth.

## Milestone 1 — Foundation

Reproducible Python 3.12 project with shared contracts:

```powershell
uv sync --locked --dev
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest -q
```

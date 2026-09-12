# Deep Research Agent

Telegram-accessible Deep Research Agent. See [`docs/PLAN.md`](docs/PLAN.md) as the source of truth.

See [`AGENTS_WORKFLOW.md`](AGENTS_WORKFLOW.md) for the bounded OpenCode agent
workflow, delegation roles, context packets, and task commit checkpoints.

## Milestone 1 — Foundation

Reproducible Python 3.12 project with shared contracts:

```powershell
uv sync --locked --dev
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest -q
```

## Milestone 4 — Safe Extraction

`research_agent.extraction.SafeExtractor` provides the M3 source handoff for
bounded HTML documents. It validates HTTP(S) URLs, rejects non-public DNS
destinations, pins validated addresses for each request, manually revalidates
redirects, respects `robots.txt`, streams within response limits, and never
executes page JavaScript. Jina Reader is disabled by default and can only be
enabled as a fallback for extraction failures through runtime settings.

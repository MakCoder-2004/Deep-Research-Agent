# Repository Context Map

This is a navigation index, not a copy of the plan. `docs/PLAN.md` remains the source of truth; load only task-relevant sections and exact line ranges.

## Architecture Boundary

The current pipeline boundary is anchored by `docs/PLAN.md` section 4, `lines 72-100`: Telegram gateway and access control feed a bounded queue; analysis produces a research plan; routing selects search/API/URL tools; normalization and ranking precede safe fetching, extraction, reader evidence, and later validation, persistence, and delivery stages.

## Areas And Paths

- Telegram access, validation, queue, progress, history, and delivery: `src/research_agent/telegram/`, `src/research_agent/services/queue.py`, `tests/unit/test_telegram_*.py`, `tests/integration/test_telegram_*.py`, `tests/security/test_telegram_auth.py`.
- Query analysis and research planning: `src/research_agent/agents/analyzer.py`, `src/research_agent/models/research.py`, `tests/unit/test_m3_analyzer.py`, `tests/unit/test_m3_planning_phase2.py`.
- Tool contracts, routing, and provider adapters: `src/research_agent/tools/`, `src/research_agent/llm/`, `tests/unit/test_llm_routing.py`, `tests/unit/test_search_adapters.py`.
- Retrieval normalization, deduplication, and ranking: `src/research_agent/ranking/`, `src/research_agent/tools/router.py`, `tests/unit/test_source_candidates.py`, `tests/unit/test_retrieval_execution_ranking.py`, `tests/integration/test_m3_retrieval.py`.
- Safe transport, DNS/SSRF controls, robots, HTML extraction, and fallback: `src/research_agent/extraction/`, `tests/unit/test_extraction.py`, `tests/security/test_extraction_security.py`, `tests/integration/test_m4_extraction.py`.
- Report models, validation, rendering, and report service: `src/research_agent/models/reports.py`, `src/research_agent/services/reports.py`, `src/research_agent/telegram/renderer.py`, `tests/unit/test_reports_validation.py`, `tests/unit/test_telegram_renderer.py`.
- SQLite persistence, retention, sessions, and observability/redaction: `src/research_agent/persistence/`, `src/research_agent/services/retention.py`, `src/research_agent/observability/`, `tests/unit/test_persistence.py`, `tests/unit/test_redaction.py`.
- Runtime entrypoint and configuration: `src/research_agent/main.py`, `src/research_agent/config.py`, `tests/unit/test_config.py`, `tests/unit/test_telegram_bot_factory.py`.

## Milestone And Plan Anchors

- Plan status and replaceable-provider rule: `docs/PLAN.md`, `lines 1-5`.
- Architecture flow and boundaries: `docs/PLAN.md`, `section 4`, `lines 72-100`.
- Existing milestone-oriented checks: M2 `tests/integration/test_m2_closeout.py`; M3 `tests/unit/test_m3_*.py` and `tests/integration/test_m3_retrieval.py`; M4 `tests/integration/test_m4_extraction.py`; closeout/hardening `tests/unit/test_closeout*.py` and `tests/unit/test_hardening.py`.
- Task IDs and ordering are defined in `docs/tasks.md`, `lines 1-5`; inspect only the task-relevant range.

## Focused Checks

- Configuration JSON: `python -m json.tool opencode.json`; if available, `opencode debug config`.
- Config Markdown: parse frontmatter delimiters and verify required agent fields; use `git diff --check` for whitespace.
- Application changes: run only the packet-named `uv run pytest`/`ruff`/`mypy` targets; provider calls remain mocked.

## Explicit Exclusions

Do not edit, stage, or commit `AGENTS.md`; do not change `skills-lock.json`, application code, tests, `docs/PLAN.md`, `docs/tasks.md`, `CONTRIBUTING.md`, or `.github/` for configuration-only work. Do not read whole docs or the repository, add packages/plugins/telemetry, invoke live providers, or add shell/code-execution/browser tools. Preserve unrelated worktree changes.

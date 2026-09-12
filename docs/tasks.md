# Deep Research Agent Tasks

Source of truth: [`docs/PLAN.md`](docs/PLAN.md)

Tasks are ordered by dependency inside each milestone. Task IDs are stable references for issues and status updates. A milestone is complete only when all required tasks and its exit gate are complete. Provider calls in automated tests must be mocked unless a manually approved live smoke test is explicitly required.

## Milestone 1: Foundation

**Goal:** Establish a reproducible Python project and the shared contracts required by later milestones.

**Depends on:** None.

### Project Bootstrap

- [x] **M1.1** Initialize the Git repository and create the planned top-level directories.
- [x] **M1.2** Add `.python-version` pinned to Python 3.12.
- [x] **M1.3** Create `pyproject.toml` with the planned runtime and development dependencies.
- [x] **M1.4** Configure Ruff formatting and linting, mypy, pytest, coverage, and package discovery in `pyproject.toml`.
- [x] **M1.5** Generate and commit `uv.lock`.
- [x] **M1.6** Create `src/research_agent/main.py` as the planned application entrypoint.
- [x] **M1.7** Add `.gitignore` entries for environments, secrets, databases, reports, caches, coverage, and build artifacts.
- [x] **M1.8** Add `.dockerignore` entries for secrets, local data, tests, caches, editor files, and build artifacts.
- [x] **M1.9** Add `.env.example` containing placeholder names only.

### Configuration

- [x] **M1.10** Implement Pydantic settings for Telegram, providers, storage, budgets, tracing, and runtime environment.
- [x] **M1.11** Parse the Telegram allowlist as numeric user IDs.
- [x] **M1.12** Validate required settings at startup without logging secret values.
- [x] **M1.13** Add configuration defaults from the plan for concurrency, quotas, timeouts, retention, and cache lifetimes.

### Domain Models

- [x] **M1.14** Define `ResearchRequest` and `ResearchPlan` models.
- [x] **M1.15** Define `SearchTask`, `SearchHit`, and `SourceDocument` models.
- [x] **M1.16** Define `EvidenceClaim`, `EvidenceLedger`, and `CritiqueResult` models.
- [x] **M1.17** Define `Finding`, `Source`, and `ResearchReport` models.
- [x] **M1.18** Define `ProviderUsage`, `ResearchJob`, `SessionContext`, and `TraceContext` models.
- [x] **M1.19** Add enums or constrained values for language, domain, depth, risk, job state, source type, and critic outcome.
- [x] **M1.20** Validate that every finding has citations and every citation refers to an existing source.
- [x] **M1.21** Validate unique canonical source URLs and source order by first citation appearance.
- [x] **M1.22** Prevent report delivery data from containing hidden prompts or chain-of-thought fields.

### Interfaces

- [x] **M1.23** Define a replaceable asynchronous LLM provider interface.
- [x] **M1.24** Define provider capability metadata for multilingual, long-context, reasoning, structured-output, Arabic, and tool-calling support.
- [x] **M1.25** Define a replaceable asynchronous research-tool interface.
- [x] **M1.26** Define normalized provider and tool error categories for routing and metrics.

### Persistence And Logging

- [x] **M1.27** Create asynchronous SQLite connection management with WAL mode.
- [x] **M1.28** Create schema initialization for users, sessions, jobs, reports, sources, tool runs, provider usage, and cache metadata.
- [x] **M1.29** Implement repositories for users and temporary sessions.
- [x] **M1.30** Implement repositories for jobs, reports, and sources.
- [x] **M1.31** Implement repositories for tool runs, provider usage, and cache metadata.
- [x] **M1.32** Enforce session retention of 24 hours or the last six interactions.
- [x] **M1.33** Make report retention configurable with an initial 90-day default.
- [x] **M1.34** Add structured JSON logging with correlation and job identifiers.
- [x] **M1.35** Redact secrets and sensitive values before writing logs.

### Verification

- [x] **M1.36** Add unit tests for settings parsing and startup validation.
- [x] **M1.37** Add unit tests for every Pydantic report and citation rule.
- [x] **M1.38** Add persistence tests for schema creation, WAL mode, repository operations, and retention behavior.
- [x] **M1.39** Add redaction tests proving configured secrets do not appear in logs.
- [x] **M1.40** Run `uv sync --locked --dev`, Ruff, mypy, and the foundation tests from a clean checkout.

### Exit Gate

- [x] **M1.GATE** A clean clone installs reproducibly with `uv sync --locked --dev`, foundational checks pass, and no runtime secret is committed. (Close-out: ruff+mypy+331 tests green, coverage 84.95%≥80, lock verified exit 0, CI workflow enforces the same on PRs, FK-complete schema with migrations, FTS triggers, tz-consistent models, redacted persistence. M3 may start.)

## Milestone 2: Telegram Gateway

**Goal:** Allow trusted Telegram users to submit, monitor, and cancel bounded research jobs.

**Depends on:** Milestone 1.

### Bot Runtime And Access

- [x] **M2.1** Create the `aiogram` 3 bot and dispatcher using long polling.
- [x] **M2.2** Wire bot startup and shutdown into `src/research_agent/main.py`.
- [x] **M2.3** Reject private-chat requests from users not present in the numeric allowlist.
- [x] **M2.4** Implement `/whoami` so administrators can obtain a numeric Telegram user ID.
- [x] **M2.5** Add a localized unauthorized-user response that reveals no configuration details.

### Commands And Input

- [x] **M2.6** Implement `/start`, `/help`, and capability and restriction messaging.
- [x] **M2.7** Implement `/research <query>` request creation.
- [x] **M2.8** Route plain text through the same path as `/research`.
- [x] **M2.9** Accept English, Arabic, mixed-language text, and HTTP(S) URLs.
- [x] **M2.10** Implement `/status` with queue position and current stage.
- [x] **M2.11** Implement `/cancel` with cooperative cancellation of the active user job.
- [x] **M2.12** Implement `/history` and `/report <id>` against local persistence.
- [x] **M2.13** Implement `/forget` for temporary session deletion.
- [x] **M2.14** Implement `/language` for English and Arabic preferences.

### Queue And Progress

- [x] **M2.15** Implement a bounded asynchronous research-job queue.
- [x] **M2.16** Enforce three globally concurrent jobs.
- [x] **M2.17** Enforce one active job per user.
- [x] **M2.18** Persist queued, active, cancelled, completed, and failed job states.
- [x] **M2.19** Publish the initial progress message when a job starts.
- [x] **M2.20** Edit the same message for analysis, source selection, search, reading, critique, and report preparation stages.
- [x] **M2.21** Handle Telegram edit and delivery failures without losing the job record.

### Rendering And Verification

- [x] **M2.22** Escape user and report content for Telegram Markdown.
- [x] **M2.23** Split oversized responses on paragraph-safe boundaries.
- [x] **M2.24** Render concise in-chat reports with readable `[1]` citation markers.
- [x] **M2.25** Attach complete long reports as `research-<report-id>.md`.
- [x] **M2.26** Add tests for allowlist enforcement and `/whoami`.
- [x] **M2.27** Add tests for commands, plain-text routing, language preferences, history, and session deletion.
- [x] **M2.28** Add tests for queue limits, cancellation, state persistence, and progress-message editing.
- [x] **M2.29** Add tests for Markdown escaping, safe splitting, and file attachment delivery.

### Exit Gate

- [x] **M2.GATE** Trusted users can submit, inspect, retrieve, and cancel jobs while unauthorized users cannot start jobs. (Close-out: 331 tests green, ruff+mypy clean, coverage 84.95%≥80, `uv sync --locked --dev` verified, CI workflow present, cancel-success ID + 9-command unauthorized matrix + 3-concurrent behavioral proven, quota reply live, retention swept hourly.)

## Milestone 3: Retrieval

**Goal:** Produce normalized, ranked source candidates from replaceable general and specialist search tools.

**Depends on:** Milestone 1.

### Research Planning

- [x] **M3.1** Implement deterministic detection of English, Arabic, and mixed-language input.
- [x] **M3.2** Classify domain, locality, jurisdiction, freshness, and risk requirements.
- [x] **M3.3** Implement deterministic quick, standard, and deep depth rules.
- [x] **M3.4** Generate focused subquestions and English and Arabic query variants when useful.
- [x] **M3.5** Select source categories, tools, and source, token, and time budgets.
- [x] **M3.6** Return a clarification request when material ambiguity prevents a reliable plan.
- [x] **M3.7** Produce and validate a structured `ResearchPlan` rather than prose.

### Provider Routing

- [x] **M3.8** Implement capability-based LLM provider registration and resolution.
- [x] **M3.9** Add configurable Groq, OpenRouter, and Cloudflare Workers AI adapters for their planned roles.
- [x] **M3.10** Add startup health checks that resolve configured capabilities to available models.
- [x] **M3.11** Keep model identifiers and provider priority in configuration rather than source constants.

### Search Adapters

- [x] **M3.12** Implement Tavily as the initial general web search adapter.
- [x] **M3.13** Implement Brave Search as the general and news fallback adapter.
- [x] **M3.14** Implement Wikipedia and Wikimedia background search.
- [x] **M3.15** Implement Semantic Scholar paper search.
- [x] **M3.16** Implement Crossref DOI and publication metadata lookup.
- [x] **M3.17** Implement arXiv search with its rate limits.
- [x] **M3.18** Implement GDELT current-event search.
- [x] **M3.19** Implement GitHub repository, release, and issue search.
- [x] **M3.20** Implement PubMed and Europe PMC biomedical search.
- [x] **M3.21** Implement direct discovery of configured official government and regulator domains.
- [x] **M3.22** Keep Exa, SerpApi, OpenAlex, Stack Exchange, SearXNG, and DDGS optional behind the common tool interface.

### Execution And Ranking

- [x] **M3.23** Route general, academic, medical, news, technical, MENA, URL, legal, and financial requests using plan-defined source priorities.
- [x] **M3.24** Run independent tool requests concurrently within per-provider limits.
- [x] **M3.25** Apply request deadlines and record every tool attempt, including failures.
- [x] **M3.26** Normalize all responses into `SearchHit` records.
- [x] **M3.27** Preserve publisher, publication date, access date, source type, and tool provenance.
- [x] **M3.28** Canonicalize URLs and remove tracking parameters.
- [x] **M3.29** Deduplicate by canonical URL, DOI, title similarity, and content hash.
- [x] **M3.30** Implement deterministic source scoring for relevance, authority, freshness, primary-source status, language or region match, corroboration, duplication, and accessibility.
- [x] **M3.31** Prefer primary and independent sources without consuming LLM quota during the first ranking pass.

### Verification

- [x] **M3.32** Add unit tests for analyzer fields, depth rules, bilingual expansion, and clarification.
- [x] **M3.33** Add mocked contract tests for each enabled LLM and search adapter.
- [x] **M3.34** Add tests for domain routing, concurrency, deadlines, and failed-tool recording.
- [x] **M3.35** Add tests for normalization, canonicalization, deduplication, metadata preservation, and source scoring.

### Exit Gate

- [x] **M3.GATE** English and Arabic test queries return normalized, deduplicated, ranked source candidates without live quota consumption in CI.

## Milestone 4: Safe Extraction

**Goal:** Convert selected URLs into bounded clean documents without exposing internal networks or executing untrusted content.

**Depends on:** Milestone 3.

### URL And Network Safety

- [ ] **M4.1** Accept only syntactically valid HTTP(S) URLs.
- [ ] **M4.2** Reject embedded credentials and unsupported schemes.
- [ ] **M4.3** Reject loopback, private, link-local, multicast, unspecified, reserved, and metadata-service IPv4 and IPv6 targets.
- [ ] **M4.4** Resolve hostnames before connection and validate every returned address.
- [ ] **M4.5** Re-resolve and revalidate the destination after every redirect.
- [ ] **M4.6** Enforce the configured redirect limit.

### Fetching

- [ ] **M4.7** Build a shared asynchronous `httpx` client with explicit connect, read, write, and pool timeouts.
- [ ] **M4.8** Send a descriptive application user agent.
- [ ] **M4.9** Respect `robots.txt` where applicable.
- [ ] **M4.10** Stream responses while enforcing maximum size before buffering full content.
- [ ] **M4.11** Reject unsupported MIME types before extraction.
- [ ] **M4.12** Normalize timeout, redirect, DNS, HTTP, size, and MIME failures into safe error categories.

### Extraction

- [ ] **M4.13** Parse allowed HTML with BeautifulSoup and lxml without executing JavaScript.
- [ ] **M4.14** Remove scripts, styles, navigation, advertisements, and repeated boilerplate.
- [ ] **M4.15** Extract title, author, publisher, publication date, headings, body text, and links.
- [ ] **M4.16** Retain short source-linked quotations for later citation verification.
- [ ] **M4.17** Bound extracted text to the configured per-source character budget.
- [ ] **M4.18** Return normalized `SourceDocument` records with fetch and extraction metadata.
- [ ] **M4.19** Implement configurable Jina Reader fallback after eligible extraction failures.
- [ ] **M4.20** Prevent permanent full-page storage and full-document tracing by default.

### Verification

- [ ] **M4.21** Add tests for schemes, credentials, localhost, private IPv4, private IPv6, link-local, multicast, and metadata addresses.
- [ ] **M4.22** Add tests for mixed DNS answers, DNS rebinding, and redirect revalidation.
- [ ] **M4.23** Add tests for redirect, timeout, response-size, and MIME limits.
- [ ] **M4.24** Add extraction fixtures for boilerplate removal, metadata, quotations, and character bounds.
- [ ] **M4.25** Add tests proving hostile page instructions remain inert data.
- [ ] **M4.26** Add tests for Jina fallback eligibility and full-content storage prevention.

### Exit Gate

- [ ] **M4.GATE** Allowed URLs produce bounded clean documents, and prohibited network targets or content fail safely before reaching agents.

## Milestone 5: Research Workflow

**Goal:** Generate validated, persisted reports through explicit LangGraph orchestration.

**Depends on:** Milestones 2, 3, and 4.

### Graph State And Nodes

- [ ] **M5.1** Define typed LangGraph state for request, plan, searches, documents, evidence, critique, report, usage, errors, and delivery.
- [ ] **M5.2** Implement `authenticate_request` and `analyze_query` nodes.
- [ ] **M5.3** Implement `clarify_query_if_required` and `build_research_plan` nodes.
- [ ] **M5.4** Implement `execute_searches`, `normalize_results`, and `rank_sources` nodes.
- [ ] **M5.5** Implement `extract_documents` and `read_evidence` nodes.
- [ ] **M5.6** Implement `critique_evidence` and conditional critic routing.
- [ ] **M5.7** Implement `run_targeted_repair_if_required` with a hard one-cycle graph limit.
- [ ] **M5.8** Implement `generate_report`, `validate_report`, `persist_report`, and `deliver_report` nodes.
- [ ] **M5.9** Add explicit terminal paths for pass, partial result, refusal, cancellation, timeout, and unrecoverable failure.
- [ ] **M5.10** Add checkpointing sufficient for job-state recovery after process interruption.

### Reader Agent

- [ ] **M5.11** Create the reader prompt and structured-output contract.
- [ ] **M5.12** Extract query-relevant claims linked to source IDs and supporting quotations.
- [ ] **M5.13** Distinguish facts, estimates, opinions, and allegations.
- [ ] **M5.14** Identify agreements, conflicts, missing dates, and uncertain authorship.
- [ ] **M5.15** Keep reader output concise enough to respect critic context budgets.
- [ ] **M5.16** Enforce prompt boundaries that treat documents as untrusted data unable to redefine tasks or invoke tools.

### Critic And Report Generation

- [ ] **M5.17** Create the critic prompt and structured `CritiqueResult` output.
- [ ] **M5.18** Check query coverage, citation support, source independence, authority, freshness, and conflicts.
- [ ] **M5.19** Check publication status, high-stakes limits, requested language, and schema compliance.
- [ ] **M5.20** Return only `PASS`, `REPAIR_REQUIRED`, `PARTIAL`, or `REFUSE` outcomes.
- [ ] **M5.21** Generate Topic, Key Findings, Summary, Sources, and Tools Used in the requested language.
- [ ] **M5.22** Populate Tools Used from recorded executions rather than model claims.
- [ ] **M5.23** Prevent persistence and Telegram delivery until Pydantic validation succeeds.
- [ ] **M5.24** Persist report metadata, source metadata, short evidence quotations, and Markdown output.

### Verification

- [ ] **M5.25** Add graph tests for node order, conditional branches, and terminal states.
- [ ] **M5.26** Add mocked tests for reader and critic structured outputs.
- [ ] **M5.27** Add tests for prompt-injection resistance and tool-execution isolation.
- [ ] **M5.28** Add tests proving invalid reports cannot be persisted or delivered.
- [ ] **M5.29** Add end-to-end mocked workflow tests for English, Arabic, URL, partial, refusal, cancellation, and recovery paths.

### Exit Gate

- [ ] **M5.GATE** A Telegram request completes the explicit graph and returns a validated, cited, persisted report in the requested language.

## Milestone 6: Observability

**Goal:** Trace each job safely without making cloud observability a runtime dependency.

**Depends on:** Milestone 5.

### Trace Controls

- [ ] **M6.1** Implement optional LangSmith initialization controlled by runtime settings.
- [ ] **M6.2** Implement development, production, sensitive, and disabled privacy modes.
- [ ] **M6.3** Default medical, legal, financial, and potentially identifying requests to sensitive mode.
- [ ] **M6.4** Create exactly one root trace for each research job.
- [ ] **M6.5** Add nested runs for analyzer, searches, ranking, extraction, reader, critic, repair, validation, persistence, and delivery.
- [ ] **M6.6** Wrap custom functions and provider SDK calls that need explicit tracing.

### Metadata And Privacy

- [ ] **M6.7** Record sanitized job, report, environment, language, domain, depth, risk, tool, source, and critic metadata.
- [ ] **M6.8** Record provider, resolved model, token, latency, cache-hit, repair-count, and sanitized error metadata.
- [ ] **M6.9** Replace Telegram user IDs with salted hashes before tracing.
- [ ] **M6.10** Redact bot tokens, API keys, authorization headers, cookies, email addresses, phone numbers, government IDs, and payment details.
- [ ] **M6.11** Prevent complete database records and full scraped documents from entering traces.
- [ ] **M6.12** Store the root trace ID with the local job record and omit it from user-visible reports.

### Resilience And Monitoring

- [ ] **M6.13** Keep local structured logs and SQLite metrics active in every trace mode.
- [ ] **M6.14** Make trace creation and upload failures non-blocking.
- [ ] **M6.15** Bound trace-upload retries.
- [ ] **M6.16** Estimate monthly root-trace usage and warn locally at 70%, 85%, and 95%.
- [ ] **M6.17** Reduce successful-run sampling near the configured allowance while prioritizing failures and deep jobs.
- [ ] **M6.18** Define dashboards for volume, success, latency, tokens, searches, provider failures, sources, critic outcomes, repairs, partial reports, language, depth, cache hits, and citation failures.

### Verification

- [ ] **M6.19** Add tests for one-root-trace structure and expected nested runs.
- [ ] **M6.20** Add tests for metadata completeness, user hashing, and secret and PII redaction.
- [ ] **M6.21** Add tests for every privacy mode and sensitive-mode suppression.
- [ ] **M6.22** Add tests for trace sampling thresholds and bounded retries.
- [ ] **M6.23** Add integration tests proving disabled or unavailable LangSmith does not fail a job.

### Exit Gate

- [ ] **M6.GATE** Every traced test job has one correctly nested root trace with no exposed sensitive data, and research succeeds without LangSmith.

## Milestone 7: Report Quality

**Goal:** Enforce evidence-backed bilingual output and bounded repair when evidence is incomplete.

**Depends on:** Milestones 5 and 6.

### Citation And Evidence Quality

- [ ] **M7.1** Implement deterministic citation existence and citation-to-source validation.
- [ ] **M7.2** Compare cited findings with retained supporting quotations.
- [ ] **M7.3** Reject findings with no citation.
- [ ] **M7.4** Normally require two authoritative or independent citations for high-stakes findings.
- [ ] **M7.5** Detect overreliance on one publisher or non-independent syndicated sources.
- [ ] **M7.6** Verify that listed tools match actual executions.

### Critique And Repair

- [ ] **M7.7** Convert critic deficiencies into focused repair queries.
- [ ] **M7.8** Search only for identified evidence gaps and prefer primary or authoritative sources.
- [ ] **M7.9** Prevent repair from rerunning the complete research plan.
- [ ] **M7.10** Bound repair to one cycle and 45 to 60 seconds.
- [ ] **M7.11** Return a clearly labeled partial report when repaired evidence remains insufficient.

### Domain And Language Policies

- [ ] **M7.12** Add authoritative-source and disclaimer policy for medical research.
- [ ] **M7.13** Add authoritative-source and disclaimer policy for legal and financial research.
- [ ] **M7.14** Add publication-status policy for academic research, including preprints, corrections, and retractions.
- [ ] **M7.15** Add freshness and conflict policy for news research.
- [ ] **M7.16** Add official-documentation and repository policy for technical research.
- [ ] **M7.17** Add bilingual search and official regional-source policy for MENA research.
- [ ] **M7.18** Refine analyzer, reader, and critic prompts for English and Arabic output.
- [ ] **M7.19** Preserve readable `[1]` citations in right-to-left Arabic reports.
- [ ] **M7.20** Communicate uncertainty, conflicts, failed tools, and reduced coverage without overstating conclusions.
- [ ] **M7.21** Refuse requests that clearly facilitate harmful or illegal activity.

### Verification

- [ ] **M7.22** Add tests for unsupported citations, missing citations, source independence, and tool accuracy.
- [ ] **M7.23** Add tests for targeted repair, repair time bounds, one-cycle enforcement, and partial reports.
- [ ] **M7.24** Add representative medical, legal, financial, academic, news, technical, and MENA policy tests.
- [ ] **M7.25** Add English and Arabic rendering and human-review fixtures.
- [ ] **M7.26** Add adversarial tests for unsupported conclusions and instructions embedded in evidence.

### Exit Gate

- [ ] **M7.GATE** Evidence-backed English and Arabic reports pass defined quality checks, and critique can trigger no more than one targeted repair.

## Milestone 8: Evaluation

**Goal:** Measure report quality and compare prompt or model changes reproducibly.

**Depends on:** Milestone 7.

### Dataset

- [ ] **M8.1** Define a versioned evaluation-case schema with query, language, category, expectations, and risk metadata.
- [ ] **M8.2** Add five general-web evaluation questions.
- [ ] **M8.3** Add five academic evaluation questions.
- [ ] **M8.4** Add five news and current-event evaluation questions.
- [ ] **M8.5** Add five technical evaluation questions.
- [ ] **M8.6** Add five MENA and Arabic evaluation questions.
- [ ] **M8.7** Add five medical, legal, and financial evaluation questions.

### Evaluators

- [ ] **M8.8** Implement deterministic citation-existence and schema-validity evaluators.
- [ ] **M8.9** Implement deterministic freshness, source-diversity, and tool-accuracy evaluators where inputs permit objective checks.
- [ ] **M8.10** Implement citation-support and query-coverage evaluators.
- [ ] **M8.11** Implement source-authority and contradiction-handling evaluators.
- [ ] **M8.12** Implement English and Arabic language-quality evaluators.
- [ ] **M8.13** Implement high-stakes compliance and unsupported-claim evaluators.
- [ ] **M8.14** Capture completion time and provider and search usage for each evaluation run.

### Experiments And Feedback

- [ ] **M8.15** Run deterministic evaluators locally without LangSmith.
- [ ] **M8.16** Upload or synchronize the fixed dataset to LangSmith when configured.
- [ ] **M8.17** Record prompt versions, provider capabilities, and resolved model identifiers for comparison.
- [ ] **M8.18** Run expensive LLM-based evaluators only in sampled or manually approved experiments.
- [ ] **M8.19** Produce comparison output for prompt and model variants.
- [ ] **M8.20** Store a mapping that allows Telegram feedback to be attached to the related report, trace, and evaluation record.

### Verification

- [ ] **M8.21** Add tests for dataset validation and category counts.
- [ ] **M8.22** Add known-pass and known-fail fixtures for deterministic evaluators.
- [ ] **M8.23** Verify that local evaluation works when LangSmith is disabled.
- [ ] **M8.24** Verify that normal CI does not invoke quota-consuming evaluators.

### Exit Gate

- [ ] **M8.GATE** The fixed 30-question suite runs reproducibly and compares prompt and model variants without expensive evaluators on every test run.

## Milestone 9: Resilience And Budget Control

**Goal:** Keep research operational and transparent when providers fail or free quotas are constrained.

**Depends on:** Milestones 5 through 8.

### Budget Enforcement

- [ ] **M9.1** Implement `BudgetManager` accounting for provider requests per minute and day.
- [ ] **M9.2** Track estimated input and output tokens and search credits.
- [ ] **M9.3** Enforce 10 requests and three deep requests per user per day.
- [ ] **M9.4** Enforce three global jobs and one active job per user through the shared budget policy.
- [ ] **M9.5** Enforce six search subqueries and 12 scraped sources per job.
- [ ] **M9.6** Enforce 20,000 extracted characters per source and an initial 40,000-character reader context.
- [ ] **M9.7** Enforce one repair cycle and a 300-second total job timeout.
- [ ] **M9.8** Persist usage counters and reset daily counters predictably.

### Cache

- [ ] **M9.9** Add normalized cache keys for provider, query, language, filters, and relevant request options.
- [ ] **M9.10** Cache general searches for six hours and news searches for 30 minutes.
- [ ] **M9.11** Cache general pages for 24 hours and news pages for one hour.
- [ ] **M9.12** Check valid cache entries before every external search or fetch.
- [ ] **M9.13** Prevent cached full pages from becoming permanent report storage.

### Fallback And Degradation

- [ ] **M9.14** Implement configured capability-based LLM fallback order.
- [ ] **M9.15** Implement configured search-tool fallback order by query type.
- [ ] **M9.16** Retry only eligible transient failures with bounded backoff.
- [ ] **M9.17** Apply provider cooldown after repeated `429` or `5xx` responses.
- [ ] **M9.18** Add circuit breakers for persistently unhealthy providers.
- [ ] **M9.19** Record provider and model fallback in job, usage, and trace metadata.
- [ ] **M9.20** Stop new external calls when job or provider budgets are exhausted.
- [ ] **M9.21** Deliver an explicit partial report when fallback cannot restore required coverage.

### Verification

- [ ] **M9.22** Add tests for every user, job, source, context, repair, and timeout limit.
- [ ] **M9.23** Add tests for cache keys, TTLs, hits, misses, and expiration.
- [ ] **M9.24** Add tests for fallback order, retry eligibility, cooldowns, and circuit breakers.
- [ ] **M9.25** Add integration tests for `429`, transient `5xx`, provider outage, quota exhaustion, and global timeout.
- [ ] **M9.26** Verify all degraded paths produce either a successful fallback or a transparent partial result.

### Exit Gate

- [ ] **M9.GATE** Simulated provider and quota failures never cause an unhandled job failure and usage remains within configured limits.

## Milestone 10: CI/CD And Oracle Deployment

**Goal:** Build, verify, publish, deploy, health-check, and roll back the service reproducibly.

**Depends on:** Milestones 1 through 9.

### Container Runtime

- [ ] **M10.1** Create a multi-stage Dockerfile targeting Linux ARM64.
- [ ] **M10.2** Install application dependencies from committed `uv.lock` inside the build.
- [ ] **M10.3** Run the application as a non-root runtime user.
- [ ] **M10.4** Keep secrets, tests, caches, reports, databases, and local environment files out of image layers.
- [ ] **M10.5** Create Docker Compose configuration for Telegram long polling.
- [ ] **M10.6** Inject secrets at runtime rather than baking them into the image.
- [ ] **M10.7** Mount SQLite and reports on persistent storage.
- [ ] **M10.8** Add a bounded health check and restart policy.

### Continuous Integration

- [ ] **M10.9** Create `.github/workflows/ci.yml` for pull requests and pushes to `main`.
- [ ] **M10.10** Install the pinned Python version and `uv` in CI.
- [ ] **M10.11** Run `uv sync --locked --dev` and fail on stale lock data.
- [ ] **M10.12** Run Ruff formatting and lint checks, then mypy.
- [ ] **M10.13** Run mocked unit, integration, and security tests with coverage.
- [ ] **M10.14** Build and smoke-test the Linux ARM64 image after application checks pass.
- [ ] **M10.15** Add secret scanning and dependency vulnerability scanning.
- [ ] **M10.16** Prevent CI from accessing live quota-limited providers, production secrets, or the production LangSmith project.
- [ ] **M10.17** Define required checks for the protected `main` branch.

### Image Publishing And Oracle Host

- [ ] **M10.18** Publish passing images to GHCR with the tested commit SHA.
- [ ] **M10.19** Record and deploy the immutable GHCR image digest.
- [ ] **M10.20** Provision an Oracle Cloud Always Free `VM.Standard.A1.Flex` Ubuntu ARM64 host.
- [ ] **M10.21** Install Docker Engine, the Compose plugin, deployment configuration, and operational tooling without host Python packages.
- [ ] **M10.22** Configure persistent storage and minimal firewall exposure for long polling.
- [ ] **M10.23** Enable operating-system security updates.
- [ ] **M10.24** Configure encrypted daily SQLite and report backups.
- [ ] **M10.25** Document and verify backup restoration.

### Deployment And Rollback

- [ ] **M10.26** Create `.github/workflows/deploy.yml` with manual dispatch for a CI-passing `main` commit.
- [ ] **M10.27** Use the protected GitHub `production` environment when available.
- [ ] **M10.28** Restrict workflow and deployment credentials to least privilege.
- [ ] **M10.29** Pull and start the exact approved digest with `docker compose up -d`.
- [ ] **M10.30** Run bounded post-deployment health verification for the Telegram worker.
- [ ] **M10.31** Restore the previously deployed digest automatically when health verification fails.
- [ ] **M10.32** Retain sanitized deployment logs.
- [ ] **M10.33** Preserve local Docker Compose as the disaster-recovery runtime.

### Verification

- [ ] **M10.34** Validate the Compose configuration and Linux ARM64 image build.
- [ ] **M10.35** Verify non-root execution and container-only startup.
- [ ] **M10.36** Inspect the image for excluded secrets, tests, caches, and local data.
- [ ] **M10.37** Restart the container and verify database and report persistence.
- [ ] **M10.38** Exercise approval, deployment, health failure, rollback, backup, and restoration paths.

### Exit Gate

- [ ] **M10.GATE** An approved, CI-passing `main` revision runs by immutable digest on Oracle Always Free and can be health-checked, backed up, restored, and rolled back.

## Milestone 11: MVP Hardening And Release

**Goal:** Demonstrate that the complete system meets the plan's security, quality, reliability, and operational acceptance criteria.

**Depends on:** Milestones 1 through 10.

### Automated Qualification

- [ ] **M11.1** Run the complete unit test suite.
- [ ] **M11.2** Run the complete mocked integration test suite.
- [ ] **M11.3** Run the complete security test suite.
- [ ] **M11.4** Run deterministic evaluation checks and the approved sampled evaluation suite.
- [ ] **M11.5** Run container and deployment tests for Linux ARM64.
- [ ] **M11.6** Run bounded load checks for two to three concurrent jobs and expected daily volume.

### Security And Privacy Review

- [ ] **M11.7** Review Telegram authorization and quota-abuse controls.
- [ ] **M11.8** Review SSRF, DNS, redirect, size, MIME, and extraction controls.
- [ ] **M11.9** Review prompt-injection boundaries and confirm no shell, code-execution, arbitrary-file, or browser tools exist.
- [ ] **M11.10** Review secret handling across source, logs, traces, SQLite, reports, images, CI, and deployment.
- [ ] **M11.11** Review container privileges, persistent storage permissions, and Oracle firewall exposure.

### Product Acceptance

- [ ] **M11.12** Human-review representative English, Arabic, MENA, and high-stakes reports.
- [ ] **M11.13** Demonstrate that at least 95% of citations resolve to listed sources.
- [ ] **M11.14** Demonstrate that at least 90% of sampled citations support their associated claims.
- [ ] **M11.15** Demonstrate that most standard jobs finish within five minutes.
- [ ] **M11.16** Confirm unauthorized users cannot start jobs.
- [ ] **M11.17** Confirm no private-network URL is fetched.
- [ ] **M11.18** Confirm provider failure produces fallback or an explicit partial report.
- [ ] **M11.19** Confirm reported tools match actual executions.
- [ ] **M11.20** Confirm no report is delivered before Pydantic validation.
- [ ] **M11.21** Confirm traced jobs have one root trace, expected nested runs, fallback metadata, token usage, and latency.
- [ ] **M11.22** Confirm LangSmith failure does not fail research and trace safeguards remain within the configured allowance.
- [ ] **M11.23** Confirm Telegram feedback can be associated with its report, trace, and evaluation record.
- [ ] **M11.24** Confirm a clean clone installs with `uv sync --locked --dev`.
- [ ] **M11.25** Confirm required pull-request checks block unverified changes.
- [ ] **M11.26** Confirm production uses an approved immutable image on Oracle Always Free without host Python packages.
- [ ] **M11.27** Confirm Compose health, restart, persistence, backup, restoration, and rollback behavior.

### Release Readiness

- [ ] **M11.28** Complete contributor, operator, configuration, backup, restoration, rollback, and incident documentation.
- [ ] **M11.29** Verify documentation references only implemented commands, settings, paths, and behavior.
- [ ] **M11.30** Record residual risks, provider assumptions, current quota checks, and known limitations.
- [ ] **M11.31** Record the trusted-user MVP release decision.

### Exit Gate

- [ ] **M11.GATE** Every MVP acceptance criterion in `docs/PLAN.md` has objective evidence and the release is approved for the trusted user group.

## Post-MVP Backlog

These items are not required to complete the MVP milestones.

- [ ] **P1** Add `/sources <report-id>` for source-only retrieval.
- [ ] **P2** Add an internal report quality indicator.
- [ ] **P3** Improve official government and regulator domain detection by country.
- [ ] **P4** Add Telegram feedback buttons and connect feedback to traces and evaluation records.
- [ ] **P5** Add PDF and Telegram document input.
- [ ] **P6** Add voice-note transcription.
- [ ] **P7** Add scheduled research, topic monitoring, and source-change alerts.
- [ ] **P8** Add PDF report export and per-user regional preferences.
- [ ] **P9** Evaluate a local Ollama fallback when suitable hardware exists.
- [ ] **P10** Add hybrid keyword and embedding retrieval if SQLite FTS5 proves inadequate.
- [ ] **P11** Add an administrator dashboard for quotas and provider health.
- [ ] **P12** Evaluate multiple specialist reader agents for very large research jobs.

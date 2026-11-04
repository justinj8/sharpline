# Sharpline Phase 1: Data Spine (Design)

- **Date:** 2026-11-04
- **Status:** Draft for review
- **Parent spec:** `2026-11-04-sharpline-design.md` (§4 data model and ingestion, §11.1 scheduling, §13 phase 1)

## 1. Purpose

Build the NBA data spine: fetch, store, normalize and replay real market and game data, so
Phase 2 (pipeline) and Phase 3 (settlement, CLV, calibration) run on real prices and
outcomes.

### 1.1 Operating model (amends the parent spec)

Sharpline is a resume project and **does not run on automation**. The parent spec assumed
daily scheduled runs; Phase 1 replaces that with **record and replay**:

- Real slates are captured **manually** in a one-day capture session run on the owner's Mac.
- Each capture is exported as a private **slate bundle**. Any environment (dev, production,
  CI fixtures) is populated by loading bundles, never by live fetching.
- The scheduler is built and tested but **off by default** (`SHARPLINE_LIVE_CAPTURE=false`).
  With it off, production never spends API credits.
- Later phases run the pipeline **on demand** (admin button / CLI) over replayed data. The
  parent spec's monthly budget therefore becomes a ceiling, not a running cost.

### 1.2 Success criteria (Phase 1 exit)

1. About three real NBA game days are captured. Each includes closing snapshots for every
   game, final results and player box scores.
2. Their bundles are pushed to the private bucket and loaded into production.
3. `quota_ledger` reports credits used per slate.
4. The `entity_review` queue is empty or fully resolved.
5. CI is green with **no network access and no API keys**.

## 2. Data sources

| Provider | Used for | Cost | Notes |
|---|---|---|---|
| The Odds API (free tier) | multi-book game lines and player props, closing prices | 500 credits/month | dedicated key in `ODDS_API_KEY`; billed per call as markets × regions |
| ESPN public endpoints | schedule, injuries, final scores | free | unauthenticated |
| nba_api (stats.nba.com) | player and team game logs (results, Phase 2 features) | free | run locally; stats.nba.com commonly blocks cloud IPs |

## 3. Architecture

### 3.1 Flow

```
CLI / capture runner / (live mode) procrastinate job
        │ executes CaptureAction
        ▼
ingest job ─► provider client ─► HttpClient (retry, backoff, Retry-After, rate limit)
   │              │ returns Fetched(payload, endpoint, params, fetched_at, quota)
   │              ▼
   │         record_source() + source_observations row + quota_ledger row
   │              │
   │         pure parser(payload) → typed DTOs
   │              │
   └────► EntityResolver → writer (upserts) → events/markets/odds_snapshots/injuries/results/player_game_stats
                         one transaction per fetch
```

**Parse on write, with pure parsers.** A parser is a pure function of a stored payload, so
`sharpline reparse` can rebuild every typed table from `sources` after a parser fix without
re-fetching.

### 3.2 Modules

| Module | Responsibility |
|---|---|
| `sources/http.py` | `HttpClient`: retries on 429/5xx and transport errors with exponential backoff, honors `Retry-After`, per-provider minimum interval |
| `sources/odds_api/client.py` | `events()`, `game_odds(markets)`, `event_odds(event_id, markets)`; parses `x-requests-last/used/remaining` |
| `sources/odds_api/markets.py` | Odds API market key → (`MarketType`, `Period`, `stat`) |
| `sources/odds_api/parse.py` | payload → `EventRow`, `MarketRow`, `OddsRow` |
| `sources/espn/{client,parse}.py` | scoreboard (schedule, finals), injuries |
| `sources/nba_stats/{client,parse}.py` | player/team game logs → `ResultRow`, `PlayerGameStatRow` |
| `ingest/resolve.py` | `EntityResolver` (§5) |
| `ingest/writer.py` | idempotent upserts of DTOs; resolution failures are skipped and queued |
| `ingest/jobs.py` | job functions invoked by `CaptureAction`s |
| `ingest/observations.py` | `latest_source(provider, endpoint, params, as_of)` |
| `capture/plan.py` | pure `plan_captures(slate, now, budget) -> CapturePlan` (§4) |
| `capture/quota.py` | `QuotaGuard` (§4.3) |
| `capture/runner.py` | executes a plan locally, persists `capture_runs` |
| `bundles/{export,load,store}.py` | slate bundle format, load-and-reparse, bucket push/pull |
| `cli.py` | `sharpline` Typer CLI (§6) |

### 3.3 Schema additions (Alembic 0003)

| Table | Columns (beyond `id`) | Notes |
|---|---|---|
| `source_observations` | `source_id → sources`, `observed_at` | one row per fetch, including deduplicated ones; index (`source_id`, `observed_at desc`) |
| `injuries` | `player_id → players`, `event_id → events null`, `status`, `detail`, `reported_at`, `source_id` | status ∈ `out`, `doubtful`, `questionable`, `probable`, `active` |
| `results` | `event_id → events` (unique), `home_score`, `away_score`, `final_at`, `source_id` | |
| `player_game_stats` | `event_id`, `player_id`, `stats jsonb`, `source_id` | unique (`event_id`, `player_id`); stat keys use the `stat` vocabulary from `markets.py` |
| `entity_review` | `provider`, `provider_id`, `entity_kind`, `raw_name`, `context jsonb`, `candidates jsonb`, `status`, `resolved_entity_id null`, `created_at` | status ∈ `open`, `resolved`, `rejected`; unique (`provider`, `entity_kind`, `provider_id`) |
| `quota_ledger` | `provider`, `cost`, `used`, `remaining`, `observed_at`, `source_id`, `capture_run_id null` | |
| `capture_runs` | `sport`, `slate_date`, `mode` (`manual`/`live`), `started_at`, `finished_at null`, `status`, `credits_cap`, `credits_used`, `plan jsonb` | status ∈ `running`, `completed`, `interrupted`, `failed` |

`source_observations` and `quota_ledger` are append-only, like `sources`.

## 4. Capture planning

### 4.1 Actions

`plan_captures(slate, now, budget)` is pure and deterministic. For an NBA slate:

| Action | When | Estimated credits |
|---|---|---|
| `schedule` | session start | 0 |
| `injuries` | start, every 30 min, T-15 per tip | 0 |
| `game_lines` | start, hourly, T-90 of first tip | 3 per call (h2h, spreads, totals × 1 region) |
| `props(event)` | start, T-90 per game | 1 × configured prop markets per game (default 6) |
| `closing(event)` | T-3 min per game; games with the same tip share one batch | game lines + props for that game |

`results` and `player_stats` are a separate next-morning command, not part of the session.

The closing snapshot moves from the parent spec's T-2 to **T-3**, which leaves room for a
slow response and one retry before tip-off.

### 4.2 Rules

- Actions scheduled before `now` are dropped. Nothing is scheduled at or after a game's
  `commence_time` (the parent spec's bettable-only-before-start invariant).
- **Budget fitting:** when the estimate exceeds `budget.session_cap`, the planner drops
  actions in this order: hourly `game_lines` refreshes → T-90 `props` → session-start
  `props`. **`closing` actions are never dropped.** If closing alone exceeds the cap, the
  plan is rejected with an explicit error. Every dropped action is recorded with a reason.

### 4.3 QuotaGuard (runtime)

- Before each metered call the guard allows it only if
  `credits_used_session + estimated_cost ≤ session_cap` **and** (when known)
  `remaining − estimated_cost ≥ floor`.
- After each call it charges the **actual** `x-requests-last` cost and writes a
  `quota_ledger` row.
- A refused call marks its action `skipped` with the reason. It is never auto-retried.
- Free-tier defaults: `session_cap = 180`, `floor = 20`.

## 5. Entity resolution

1. Exact `entity_xref (provider, kind, provider_id)` hit → use it.
2. Otherwise, a deterministic match on normalized name plus sport (plus team when known):
   - **Normalization:** NFKD, accents stripped, lowercased, punctuation removed, suffixes
     (`jr`, `sr`, `ii`, `iii`, `iv`) removed, whitespace collapsed.
   - Exactly one candidate → create the xref automatically.
3. Several candidates, or only a fuzzy candidate (`rapidfuzz` token-sort ratio ≥ 90) →
   upsert an `entity_review` row and **skip the row being written**. Unknown names with no
   candidate are queued the same way.
4. NBA teams (30) are seeded with their ESPN, Odds API (full name) and nba_api IDs, so team
   resolution is always exact.

## 6. Bundles, replay and CLI

### 6.1 Slate bundle

- **File:** `<sport>-<slate_date>-<sha8>.jsonl.gz`.
- **Line 1** is the manifest: `format_version=1`, `sport`, `slate_date`, `capture_run_id`,
  `alembic_revision`, `sharpline_version`, `counts`, `content_sha256`.
- **The remaining lines** are `{"kind": "source", ...}` and `{"kind": "observation", ...}`
  records.

**Load:**
- verifies the manifest hash;
- inserts sources through `record_source` semantics, so loading is idempotent and keeps
  the original `fetched_at`;
- inserts observations;
- runs reparse for the loaded sources.

**Storage:** a private Railway bucket declared in `.railway/railway.ts`. Credentials come
only from environment variables. The public repo holds only trimmed fixtures (a few events
per provider) for tests.

### 6.2 CLI

```
sharpline capture nba --date YYYY-MM-DD [--cap 180] [--dry-run]
sharpline results nba --date YYYY-MM-DD
sharpline backfill nba-stats --seasons 2023-24 2024-25 2025-26
sharpline bundle export <capture_run_id> | push <file> | pull <name> | load <file>
sharpline reparse [--provider odds_api|espn|nba_stats]
sharpline review list | review resolve <review_id> <entity_id> | review reject <review_id>
sharpline quota
```

`--dry-run` prints the plan, its estimated credits and any dropped actions, with no
network calls.

### 6.3 Live mode

`Settings.live_capture` (env `SHARPLINE_LIVE_CAPTURE`, default `false`). Only when it is
true does the worker register a periodic `plan_and_enqueue` job, which runs the same
`plan_captures` and enqueues each action as a procrastinate job.

## 7. Error handling

- Every action ends `done`, `skipped(reason)` or `failed(error)`, recorded in
  `capture_runs.plan`.
- **Per-provider circuit breaker:** 3 consecutive failures skip that provider's actions for
  10 minutes; other providers continue.
- **Missing `ODDS_API_KEY`:** odds actions are skipped with reason `no_api_key`; ESPN and
  nba_api still run.
- **SIGINT/SIGTERM:** the run is marked `interrupted` and a partial bundle is still
  exported.
- A parser error on one payload fails that action only. The source row is still recorded,
  so a fixed parser can reparse it later.

## 8. Testing

All of this runs in CI with no network access and no API keys.

- **Parsers:** run against trimmed real fixtures in `tests/fixtures/<provider>/`.
- **Clients:** `httpx.MockTransport` covers quota-header parsing, retry/backoff,
  `Retry-After` and the circuit breaker.
- **`plan_captures`:** frozen-clock tests, plus Hypothesis properties — closing is never
  dropped, no action is at or after its game's tip, and the estimate never exceeds the cap.
- **`QuotaGuard`:** floor, cap and actual-versus-estimated charging.
- **Resolver:** normalization cases (accents, Jr./III, punctuation), ambiguous names going
  to review, team seeds.
- **Integration (Postgres):**
  - the writer is idempotent;
  - `latest_source` handles A→B→A;
  - **round trip:** export → load into a fresh database → reparse produces identical typed
    rows.

## 9. Out of scope

- NFL and MLB clients (Phases 5–6).
- Feature engineering and models (Phase 2).
- Settlement and CLV computation (Phase 3); this phase only stores what they need.
- The admin UI for the review queue (Phase 4); the CLI covers it until then.
- Any always-on automation.

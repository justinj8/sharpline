# Sharpline — Design Spec

- **Date:** 2026-11-04
- **Status:** Draft for review
- **Owner:** Justin John

## 1. Purpose and success criteria

Sharpline unifies the owner's daily MLB, NBA and NFL betting sheets and Linewatch into one engineered, multi-sport agentic forecasting platform. It is a resume showcase: it must demonstrate full-stack system design and, above all, **measured cost, accuracy and calibration**.

Success means:

1. A deployed backend (Railway) and frontend (Vercel) that produce a daily card per in-season sport without manual steps.
2. Every published pick traces to fetched sources; unverifiable picks are dropped by code, not by prompt.
3. Every run records its cost; every graded pick contributes to Brier, CLV, ROI and calibration metrics shown publicly with confidence intervals.
4. Adding a sport means implementing one `SportModule` interface.

**Non-goals:** real-money bet placement, multi-user accounts, subscriptions, betting advice. All tracking is paper tracking for research.

## 2. Locked decisions

| Area | Decision |
|---|---|
| Repo | Fresh monorepo; ideas ported selectively from `linewatch`, `nba_prediction_system`, `ai-nfl-model`, `mlb_prediction_system`, `mlb-daily-sheet`. No wholesale code import. |
| Architecture | Modular monolith: FastAPI `api` + `worker` processes from one image; one LangGraph graph over a `SportModule` protocol; Postgres for data, job queue and checkpoints |
| Data | The Odds API + ESPN + MLB StatsAPI/Statcast + nba_api + nflverse |
| Probability | Quant base → bounded Sonnet adjustment (±5pp, or veto) → fitted calibrator |
| Markets | Game lines and player props from day one |
| Parlays | Named tiers with per-sport whitelist (see §6.4) |
| Grounding | Odds/injury/projection failures drop the pick; narrative failures are stripped and the adjustment citing them is reset to 0pp |
| Cold start | Forward-only calibration; quant models train on free historical box scores |
| Frontend | Public showcase + GitHub-OAuth admin for a single operator |
| Deploy | Railway (api, worker, Postgres+pgvector) + Vercel (Next.js) |
| Delivery | Web + HTML email |
| Budget | ~$100/month total (LLM + Odds API + hosting); ~$0.80 LLM cap per sport run |
| Build order | NBA → NFL → MLB (MLB ready before Opening Day, late March 2027) |

## 3. System architecture

```
External sources (Odds API, ESPN, StatsAPI, nba_api, nflverse)
        │
        ▼
Worker (Railway): scheduler · ingestion · LangGraph pipeline · grading/calibration · email
        │
        ▼
Postgres 16 + pgvector (Railway): sources, odds, picks, grades, ledger, queue, checkpoints
        │
        ▼
FastAPI api (Railway) ──► Next.js web (Vercel, ISR + admin)
```

- `api` serves public reads and authenticated admin actions.
- `worker` runs procrastinate (Postgres-backed queue) periodic and on-demand jobs.
- Both import the single `sharpline` Python package, so domain types exist once.

### 3.1 Repository layout

```
sharpline/
├── apps/
│   ├── api/            FastAPI app: public + /admin routers
│   ├── worker/         scheduler entrypoint, job definitions
│   └── web/            Next.js 15 (App Router, TypeScript, Tailwind, shadcn/ui)
├── src/sharpline/
│   ├── domain/         Event, Market, Selection, Price, Source, Claim, Pick, Slip
│   ├── sources/        odds_api, espn, mlb_stats, nba_stats, nflverse clients
│   ├── sports/         base.py (SportModule protocol) + nba/ mlb/ nfl/
│   ├── pipeline/       LangGraph graph, nodes, state, model router
│   ├── grounding/      claim verification gate
│   ├── calibration/    calibrators + metrics
│   ├── settlement/     grading rules per market
│   ├── ledger/         token/cost accounting, budget guard
│   └── delivery/       card rendering (web JSON, HTML email)
├── db/migrations/      Alembic
├── infra/              docker-compose (local), Railway config
├── docs/               ADRs, specs, plans
└── tests/              unit / integration / replay / evals
```

### 3.2 Stack

Python 3.12, uv, FastAPI, SQLAlchemy 2 + Alembic, Postgres 16 + pgvector, LangGraph + langchain-anthropic, procrastinate, scikit-learn, LightGBM, Pydantic v2, structlog, tenacity. Frontend: Next.js 15, TypeScript, Tailwind, shadcn/ui, Recharts, Auth.js, OpenAPI-generated TS client. Tooling: ruff, mypy, pytest, Hypothesis, testcontainers, Vitest, Playwright, GitHub Actions, gitleaks, Langfuse, Sentry.

## 4. Data model and ingestion

### 4.1 Provenance principle

Every fetched payload is first written to an append-only `sources` row: `provider, endpoint, params, fetched_at, content_hash, payload (jsonb)`, deduplicated on `(provider, endpoint, params, content_hash)`. All derived rows carry a `source_id`. Runs read only sources with `fetched_at <= run_started_at`, which makes runs reproducible and leak-safe. Parsers can be fixed and history re-derived without re-fetching.

### 4.2 Tables

| Group | Tables | Notes |
|---|---|---|
| Provenance | `sources` | immutable, hashed |
| Entities | `teams`, `players`, `entity_xref`, `entity_review` | canonical IDs mapped to each provider's IDs; fuzzy matches queued for review, never auto-accepted |
| Slate | `events`, `markets` | `markets.type` ∈ moneyline, spread, total, player_prop, special; plus `stat`, `player_id`, `line`, `period` (FULL/H1/Q1/F5/I1) |
| Prices | `odds_snapshots` | `market_id, book, side, price, line, captured_at, source_id, is_closing` |
| Context | `injuries`, `features`, `model_outputs` | features versioned by `feature_set_version` and stamped `as_of` |
| Pipeline | `runs`, `run_steps`, `candidates`, `claims`, `picks`, `slips`, `slip_legs`, `drops` | picks store `p_quant`, `p_adjusted`, `p_final` separately |
| Eval | `results`, `grades` (versioned), `calibrators`, `metric_snapshots` | |
| Cost | `cost_ledger` | one row per LLM call: run, node, model, input/output/cache-read/cache-write tokens, USD |
| Ops | `config_versions`, `audit_log`, `schedule_stamps` | |

### 4.3 Ingestion cadence (game days only)

- **Morning (~8am ET):** schedules, rosters, stats/features, opening game-line odds.
- **Injuries:** ESPN every 30 min; forced re-fetch before publish.
- **Odds:** game lines hourly, every 10 min inside T-90min. Props are fetched for the slate before the run. **Closing snapshot at T-2min only for markets with picks.**
- **Results:** nightly and next morning; re-grade window 72 h for stat corrections.
- **QuotaGuard:** tracks Odds API credits from response headers against a monthly budget (~20K credits tier). It degrades in this order: fewer books → fewer prop markets → game lines only. It never fails a run silently.

### 4.4 Market catalog (Odds API keys)

- **NBA props:** points, rebounds, assists, threes, steals, blocks, turnovers, PRA, P+R, P+A, R+A, double-double. Game: h2h, spreads, totals.
- **NFL props:** pass yds/attempts/completions/TDs/INTs, rush yds/attempts, receptions, reception yds, anytime/1st/last TD. Game: h2h, spreads, totals, plus H1 and Q1 variants.
- **MLB props:** batter hits, total bases, home runs, RBIs, runs, stolen bases, walks, singles, doubles, strikeouts, H+R+RBI; pitcher strikeouts, hits allowed, walks, earned runs, outs. Game: h2h, spreads, totals, F5 variants, NRFI/YRFI (1st-inning total under/over 0.5).

## 5. Pipeline

### 5.1 Graph (one run per sport per slate)

| # | Node | Executor | Responsibility |
|---|---|---|---|
| 1 | `load_slate` | code | events, markets, latest odds, features as of run time |
| 2 | `quant_screen` | code | `p_quant` per selection; no-vig market probability (Pinnacle if quoted, else multi-book consensus); keep top-K by edge (~40 props + qualifying game lines) |
| 3 | `research` | Haiku | per-game fan-out (`Send`); reads stored sources; emits `Claim`s via tool schema |
| 4 | `analyze` | Sonnet | per candidate: adjustment in [-5pp, +5pp] or veto; rationale cites claim IDs only |
| 5 | `ground` | code | verification gate (§7) |
| 6 | `calibrate` | code | `p_final = calibrator[sport, family](p_adjusted)` |
| 7 | `build_slips` | code | leg grades, singles by EV, tiered parlay candidates, Monte Carlo joint probability, Kelly sizing and caps |
| 8 | `thesis` | Sonnet | chooses among top deterministic combos per tier and writes a falsifiable game-script thesis; cannot add legs |
| 9 | `write_card` | Opus | narrative card from structured picks; text re-grounded; falls back to the template renderer on failure |
| 10 | `publish` | code | persist, render web JSON + HTML email, send, revalidate the web cache |

### 5.2 SportModule protocol

```python
class SportModule(Protocol):
    sport: Sport
    def markets(self) -> list[MarketSpec]
    def ingest_jobs(self) -> list[JobSpec]
    def schedule(self) -> list[ScheduleSlot]
    def build_features(self, slate: Slate, as_of: datetime) -> FeatureFrame
    def model(self, features: FeatureFrame) -> list[QuantEstimate]
    def research_prompt(self) -> PromptPack
    def card_sections(self) -> list[CardSection]
    def parlay_policy(self) -> ParlayPolicy
    def settle(self, pick: Pick, result: EventResult) -> Settlement
```

### 5.3 Model routing and cost

- `ModelRouter` maps task → model from config: `extract → claude-haiku-5-5`, `analyze`/`thesis → claude-sonnet-5-5`, `card → claude-opus-5-5`.
- Every call writes a `cost_ledger` row computed from API `usage` × a versioned price table.
- **Caps:** ~$0.80 per sport run plus a daily cap. If a cap is at risk, the router degrades in this order: Opus card → Sonnet card → template card; Sonnet skips low-edge candidates. Budget-capped calls are never auto-retried.
- Prompt caching is applied to system prompts and shared slate context.
- LangGraph `PostgresSaver` checkpoints every node; runs are idempotent per `(sport, slate_date, run_kind)` and resumable.

## 6. Card and slips

### 6.1 Card sections

1. Data freshness and injury screen (timestamps per source)
2. Sport specials: MLB Top 10 NRFI and Top 10 HR props; NFL TD scorer board; NBA none initially
3. Top 10 player props by calibrated edge
4. Game-line picks
5. Core parlays: A/B legs only, max 3 legs
6. Tiered parlays with theses
7. Bankroll allocation

### 6.2 Leg grades

A: `p_final ≥ 0.60`; B: `≥ 0.55`; C: `≥ 0.52`. C legs are never parlay-eligible.

### 6.3 Sizing

20% fractional Kelly, unit-based, with exposure caps per game, per player, per day (and per week for NFL, read back from the ledger). Minimum EV and probability buffer are configurable.

### 6.4 Parlay tiers

| Tier | Odds range | Legs |
|---|---|---|
| lock | +100 to +350 | 2 |
| value | +250 to +650 | 3 |
| moonshot | +500 to +1500 | 3–4 |
| ambitious | +1000 to +3000 | 4 |
| longshot | +2500 to +5000 | 5 |

Active whitelist: **MLB** lock, value, moonshot, longshot. **NBA** lock, value, moonshot, ambitious. **NFL** singles only until validated. Diversification per tier: max legs per player, minimum distinct games, maximum pairwise correlation (empirical same-game matrix). A tier is left empty rather than padded with weak legs. Every tier is graded separately against its implied probability.

## 7. Grounding gate

### 7.1 Claims

Sources are shown to the model under short handles (`S12`) that the server maps to real IDs. Claims are emitted through a tool schema with `claim_id, type, subject, asserted, source, locator (json_pointer or span), quote`. Rationales cite claim IDs only.

### 7.2 Verification

| Claim type | Verified when | Freshness |
|---|---|---|
| `odds` | exact match to an `odds_snapshots` row from that source (book, side, line, price) | ≤ 30 min at run; re-checked at publish |
| `injury` | equals the parsed `injuries` row and no newer source contradicts | ≤ 3 h; forced re-fetch before publish |
| `projection` / `stat` | value at the locator (or our `model_outputs` row) equals the asserted value within rounding tolerance | feature `as_of` |
| `narrative` | normalized quote appears verbatim in the payload | ≤ 24 h |

### 7.3 Pick rules

A pick survives only if:

1. it has a verified price claim for the exact market (and, for props, a verified availability claim);
2. every cited odds, injury and projection claim verifies;
3. the rationale's number scan finds no figure absent from verified claims or pick fields.

Narrative failures strip the claim and reset the adjustment that cited it to 0pp. Hard failures drop the pick with a reason code (`UNSOURCED_NUMBER`, `STALE_ODDS`, `INJURY_CONTRADICTED`, `MISSING_PRICE_CLAIM`, …). The card text from `write_card` passes the same gate. Drop rate by reason is a public metric.

## 8. Settlement, CLV, calibration and metrics

### 8.1 Settlement

- Outcomes are `win/loss/push/void/unresolved`; `unresolved` is used whenever data is missing, never a guess.
- DNP → void. MLB pitcher props void if he does not start.
- Parlays drop void and push legs and recompute the payout.
- Grades are versioned; stat corrections re-grade within 72 h.
- Post-publish ruled-out players → `VOID_PREGAME`.

### 8.2 CLV

- Price CLV = `decimal_taken / decimal_close_novig − 1`, against the T-2min closing snapshot.
- Line CLV in points for spreads and totals.
- Parlays use per-leg CLV.

### 8.3 Metrics

- Brier for `p_quant`, `p_adjusted` and `p_final`.
- **Brier skill score vs. no-vig market probability.**
- Log loss, ECE, reliability bins.
- Hit rate vs. implied by tier, family and sport.
- ROI, units, mean CLV, % beating the close.
- $/run, $/pick, $/graded pick, cache hit rate, grounding drop rate.
- All rates carry game-clustered bootstrap CIs.

### 8.4 Calibrators

- One per `(sport, market_family)`, over and under separate.
- Platt below 500 samples, isotonic at or above.
- Hierarchical fallback family → sport → global → identity below 100 samples. The UI shows "calibrating n/100".
- Weekly refit on time-ordered data; the candidate is scored on the most recent held-out window and stored in shadow. **Promotion is manual** via admin.

## 9. API

Public (`/v1`, read-only, rate-limited):

- `GET /cards`, `/cards/{id}`, `/picks/{id}` (with claims + source snippets)
- `GET /metrics/summary|calibration|clv|tiers`
- `GET /runs`, `/runs/{id}/trace`
- `GET /health`

Admin (`/v1/admin`):

- `POST /runs`, `/runs/{id}/resume`
- `GET /drops`
- `GET/PUT /config` (versioned, audited)
- `POST /calibrators/{id}/promote`
- `GET/POST /entity-review`

**Auth:** Auth.js GitHub OAuth restricted to the owner's login. Next.js server routes call admin endpoints with a short-lived JWT verified by FastAPI. The public API uses a read-only DB role.

## 10. Frontend

- `/`: today's cards, headline metrics, architecture explainer
- `/cards/[sport]/[date]`: full card; every pick expands into provenance (claim → source snippet → fetched_at)
- `/track-record`: equity curve, ROI/hit rate vs. implied by tier, sport and market
- `/calibration`: reliability diagrams, Brier by layer over time, skill vs. market
- `/runs/[id]`: pipeline trace viewer (node timeline, model, tokens, cost, drops)
- `/cost`: spend vs. caps, $/pick, cache hit rate
- `/admin/*`: run controls, drops, entity review, config editor, calibrator promotion

Public pages use ISR, revalidated by a webhook on publish.

## 11. Operations

### 11.1 Scheduling

A procrastinate tick every 5 min evaluates due slots in `America/New_York` with per-`(sport, date, slot)` stamps (idempotent, catches up after downtime). Default slots:

- **NBA:** card ~2pm ET daily
- **NFL:** Thu/Sun/Mon 10am ET (Sat late season), grading Tuesday
- **MLB:** card 11:30am ET

Invariant: only games with `commence_time > publish_time` are pickable.

### 11.2 Error handling

- **Sources:** backoff + jitter retries and a per-provider circuit breaker. Partial data publishes with a disclosure and excluded markets. Missing odds → no card + alert email.
- **LLM:** retry on 429/5xx; one repair attempt on invalid structured output, then drop.
- **Sanity guards:** a prediction-collapse detector aborts the run; feature-coverage checks run before modeling.

### 11.3 Observability

structlog JSON logs keyed by `run_id`, Langfuse traces, Sentry, and run/metric tables backing `/runs` and `/cost`.

### 11.4 Security

Secrets only in Railway/Vercel environment variables, gitleaks in CI, rate limiting, admin audit log, and a site-wide research disclaimer.

## 12. Testing

- **Unit:** table-driven settlement per market, grounding verifiers, odds math, calibrators, Hypothesis property tests for parlay constraints.
- **Integration:** Postgres via testcontainers, API contract tests.
- **Pipeline replay:** recorded source cassettes + fake chat model → deterministic full-graph run with golden card snapshot (every PR, $0).
- **LLM evals** (manual/nightly): claim-extraction accuracy; adversarial grounding cases with injected fabrications that must be dropped.
- **Frontend:** Vitest components, Playwright e2e against a seeded API.

### CI/CD

GitHub Actions runs ruff, mypy (strict on `src/`), eslint, tsc, tests, gitleaks, the OpenAPI client drift check and the Docker build. Railway deploys `main` with Alembic in a release step; Vercel builds a preview per PR.

## 13. Build phases

| Phase | Deliverable |
|---|---|
| 0 | Repo, tooling, CI, docker-compose, core schema, domain types, `/health` deployed |
| 1 | Data spine: `sources`, Odds API + ESPN + nba_api clients, QuotaGuard, entity resolution, scheduler, odds snapshots |
| 2 | NBA pipeline end to end: quant model, graph, router + ledger, grounding, slips, email card |
| 3 | Settlement, CLV, metrics, calibrators (shadow + promote) |
| 4 | API + Next.js public site + admin |
| 5 | NFL module (TD board, H1/Q1) |
| 6 | MLB module (NRFI, F5, HR board) before Opening Day |
| 7 | Hardening: evals, Langfuse/Sentry, README with live metrics, ADRs, demo video |

Each phase gets its own implementation plan and ends deployed and demoable.

## 14. Risks

| Risk | Mitigation |
|---|---|
| Odds API credit exhaustion | QuotaGuard degradation order; closing snapshots only for picked markets |
| nba_api / ESPN unofficial endpoints change or rate-limit | source cassettes in tests; circuit breakers; parsers isolated per provider |
| Small samples make metrics noisy | bootstrap CIs; "calibrating n/100" badges; no claims without CIs |
| LLM adds noise rather than signal | measured by Brier(p_quant) vs. Brier(p_adjusted); adjustment can be disabled per family in config |
| Entity mismatches across providers | xref table + review queue; never auto-accept fuzzy matches |

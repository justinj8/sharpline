# Phase 1: Data Spine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture real NBA slates manually (game-line and prop odds incl. closing, schedule, injuries, results, player stats) into `sources` and typed tables, export them as private slate bundles, and replay them into any environment, with CI fully offline.

**Architecture:**
- Provider clients return `Fetched` payloads. A job records each one (`record_source` + an observation + a quota row), runs a **pure parser**, and writes typed rows through an `EntityResolver`, all in one transaction.
- A pure `plan_captures` function decides what to fetch and when. A local runner executes the plan, and live mode (off by default) enqueues the same actions on procrastinate.
- Bundles are gzipped JSONL of sources plus observations; loading one ends with a reparse.

**Tech Stack:**
- Existing: Python 3.12, uv, SQLAlchemy 2, Alembic, httpx, procrastinate, pydantic.
- New dependencies: `typer==0.27.3`, `rapidfuzz==3.14.6`, `nba_api==1.12.0`, `boto3==1.43.123`; dev-only `hypothesis==6.170.14`.

**Spec:** `docs/superpowers/specs/2026-11-04-phase-1-data-spine-design.md` (parent: `2026-11-04-sharpline-design.md`)

## Global Constraints

- **CI makes no network calls and needs no API keys.** Every client test uses `httpx.MockTransport` or recorded fixtures under `tests/fixtures/<provider>/`.
- **Public-repo fixtures are trimmed:** at most 2 events per Odds API fixture, at most 2 teams per ESPN injuries fixture, at most 5 rows per nba_api fixture.
- All timestamps are timezone-aware UTC (`ensure_utc`). Slate dates are interpreted in `America/New_York`.
- `sources`, `source_observations` and `quota_ledger` are append-only. The DB rejects UPDATE/DELETE on the first two; `quota_ledger` is insert-only by convention.
- Odds are requested with `oddsFormat=american` and `regions=us`. Game markets are `h2h,spreads,totals`.
- **Default NBA prop markets** (6): `player_points`, `player_rebounds`, `player_assists`, `player_threes`, `player_points_rebounds_assists`, `player_blocks`.
- **Capture defaults:** `session_cap=180` credits, `floor=20`, closing at **T-3 min**, injuries every 30 min plus T-15, game lines hourly plus T-90 of the first tip, props at session start plus T-90 per game.
- **Budget drop order:** hourly `game_lines` → T-90 `props` → session-start `props`. `closing` is never dropped.
- **Entity resolution:** exact xref → unique normalized-name match (auto xref) → otherwise `entity_review` and skip. Fuzzy candidates (`rapidfuzz.fuzz.token_sort_ratio ≥ 90`) are listed in review, never auto-accepted.
- Secrets (`ODDS_API_KEY`, bucket credentials) are `SecretStr`, read only from env, and never printed. Empty env values count as unset.
- `SHARPLINE_LIVE_CAPTURE` defaults to `false`; with it false the worker registers no capture job.

## Review Focus

1. An empty `ODDS_API_KEY=` copied from `.env.example` must count as **missing**: odds actions are skipped with `no_api_key`, and no unauthenticated requests are sent. → Task 1 (`env_ignore_empty`) and Task 9.
2. Team names differ across providers (ESPN `LA Clippers`/`LAC`, Odds API `Los Angeles Clippers`, nba_api `LAC`; ESPN `GS`/`NY`/`SA`/`NO`/`UTAH`/`WSH` vs `GSW`/`NYK`/`SAS`/`NOP`/`UTA`/`WAS`). All 30 must resolve exactly. → Task 6.
3. A game whose tip time moves (or that is postponed) between the schedule fetch and its closing action must close at the **new** time or be skipped, never priced after the real tip. → Task 9.
4. The Odds API returning 401/429 or a quota-exhausted response mid-session marks the remaining odds actions `skipped`/`failed`. The session continues for ESPN and still exports a bundle. → Task 9.
5. Different books quoting the same player prop at **different lines** (Over 27.5 at DK, Over 28.5 at FD) become separate markets and keep both prices. → Task 7.

---

## File Structure

```
src/sharpline/config.py                     + live_capture, capture_*, bundle_* settings; env_ignore_empty
src/sharpline/db/models/ingest.py           SourceObservation, Injury, Result, PlayerGameStat, EntityReview, QuotaLedger, CaptureRun
db/migrations/versions/0003_ingest_tables.py
src/sharpline/sources/fetched.py            Fetched, QuotaInfo
src/sharpline/sources/http.py               HttpClient, CircuitBreaker, ProviderUnavailable
src/sharpline/sources/odds_api/{markets,client,parse}.py
src/sharpline/sources/espn/{client,parse}.py
src/sharpline/sources/nba_stats/{client,parse}.py
src/sharpline/sources/rows.py               DTOs: EventRow, OddsRow, InjuryRow, ResultRow, PlayerGameStatRow
src/sharpline/sports/nba/teams.py           NBA_TEAMS seed data + seed_nba_teams(session)
src/sharpline/ingest/{normalize,resolve,writer,observations,jobs,reparse}.py
src/sharpline/capture/{plan,quota,runner}.py
src/sharpline/bundles/{format,export,load,store}.py
src/sharpline/cli.py                        Typer app; console script `sharpline`
apps/worker/src/sharpline_worker/app.py     + live-mode registration
.railway/railway.ts                         + private bucket
docs/runbooks/capture.md
tests/fixtures/{odds_api,espn,nba_stats}/...
```

---

### Task 1: Settings, dependencies and ingest schema (Alembic 0003)

**Files:**
- Modify: `src/sharpline/config.py`, `pyproject.toml`, `src/sharpline/db/models/__init__.py`
- Create: `src/sharpline/db/models/ingest.py`, `db/migrations/versions/0003_ingest_tables.py`
- Test: `tests/unit/test_config.py`, `tests/integration/test_ingest_schema.py`

**Interfaces:**
- Produces:
  - `Settings` gains these fields, plus `env_ignore_empty=True` in `model_config`:
    - `live_capture: bool = False` (env `SHARPLINE_LIVE_CAPTURE`)
    - `capture_session_cap: int = 180`
    - `capture_quota_floor: int = 20`
    - `bundle_bucket: str | None = None`
    - `bundle_endpoint_url: str | None = None`
    - `bundle_region: str = "auto"`
    - `bundle_access_key_id: SecretStr | None = None`
    - `bundle_secret_access_key: SecretStr | None = None`
  - ORM models in `db/models/ingest.py` matching spec §3.3:
    - `SourceObservation(source_id, observed_at, purpose: str | None)` (`purpose` CHECK ∈ {closing} or NULL)
    - `Injury(player_id, event_id|None, status, detail|None, reported_at, source_id)`
    - `Result(event_id unique, home_score, away_score, final_at, source_id)`
    - `PlayerGameStat(event_id, player_id, stats: dict, source_id)`, unique (`event_id`, `player_id`)
    - `EntityReview(provider, provider_id, entity_kind, raw_name, context: dict, candidates: list, status='open', resolved_entity_id|None, created_at)`, unique (`provider`, `entity_kind`, `provider_id`)
    - `QuotaLedger(provider, cost, used|None, remaining|None, observed_at, source_id|None, capture_run_id|None)`
    - `CaptureRun(sport, slate_date: date, mode, started_at, finished_at|None, status, credits_cap, credits_used=0, plan: dict)`
  - CHECK constraints:
    - `Injury.status` ∈ {out, doubtful, questionable, probable, active}
    - `EntityReview.status` ∈ {open, resolved, rejected}
    - `CaptureRun.mode` ∈ {manual, live}
    - `CaptureRun.status` ∈ {running, completed, interrupted, failed}
  - Migration `0003` (down_revision `0002`): autogenerate the DDL, then hand-add a trigger reusing `sources_immutable()` on `source_observations`.

- [ ] **Step 1: Add dependencies** with `uv add` at the exact versions in Tech Stack (typer, rapidfuzz, nba_api, boto3 in the core package; hypothesis in the dev group). Run `uv lock`.
- [ ] **Step 2: Write failing tests**

```python
# tests/unit/test_config.py (append)
def test_empty_secret_env_counts_as_unset(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h/d")
    monkeypatch.setenv("ODDS_API_KEY", "")
    assert Settings(_env_file=None).odds_api_key is None

def test_capture_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h/d")
    s = Settings(_env_file=None)
    assert (s.live_capture, s.capture_session_cap, s.capture_quota_floor) == (False, 180, 20)

# tests/integration/test_ingest_schema.py  (pytestmark integration)
def test_ingest_tables_exist(db_session):
    assert {"source_observations", "injuries", "results", "player_game_stats",
            "entity_review", "quota_ledger", "capture_runs"} <= set(inspect(db_session.get_bind()).get_table_names())

def test_source_observations_are_immutable(db_session, seeded_source):
    db_session.add(SourceObservation(source_id=seeded_source, observed_at=datetime.now(UTC)))
    db_session.flush()
    with pytest.raises(DBAPIError, match="immutable"):
        db_session.execute(text("delete from source_observations"))

def test_injury_status_check(db_session, seeded_source, seeded_player):
    db_session.add(Injury(player_id=seeded_player, status="day-to-day", reported_at=datetime.now(UTC), source_id=seeded_source))
    with pytest.raises(IntegrityError):
        db_session.flush()
```

Add a `seeded_player` fixture to `tests/conftest.py` (one `Player`, sport `nba`).
- [ ] **Step 3: Run** `uv run pytest tests/unit/test_config.py tests/integration/test_ingest_schema.py -q`. Expected: FAIL.
- [ ] **Step 4: Implement** the settings, models and migration. Generate `0003` via `alembic revision --autogenerate --rev-id 0003` against a throwaway database, as for 0001, then add the trigger.
- [ ] **Step 5: Run** `uv run pytest tests -q && make lint typecheck`. Expected: PASS, including `test_models_match_migrations`.
- [ ] **Step 6: Commit:** `feat(db): ingest tables (observations, injuries, results, stats, review, quota, capture runs)`

---

### Task 2: HttpClient, Fetched and the circuit breaker

**Files:**
- Create: `src/sharpline/sources/__init__.py`, `src/sharpline/sources/fetched.py`, `src/sharpline/sources/http.py`
- Test: `tests/unit/sources/test_http.py`

**Interfaces:**
- Produces:
  - `QuotaInfo(last: int | None, used: int | None, remaining: int | None)`, a frozen dataclass.
  - `Fetched(provider: str, endpoint: str, params: dict[str, Any], payload: Any, fetched_at: datetime, quota: QuotaInfo | None = None)`, a frozen dataclass.
  - `HttpClient(base_url: str, *, timeout: float = 15.0, max_retries: int = 3, backoff_base: float = 0.5, min_interval: float = 0.0, transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], datetime] = utcnow)`
    - `.get(path: str, params: dict[str, Any]) -> httpx.Response` retries 429/500/502/503/504 and transport errors. It honors integer `Retry-After` seconds, otherwise sleeps `backoff_base * 2**attempt`. Non-retryable 4xx raise `httpx.HTTPStatusError` immediately.
  - `CircuitBreaker(threshold: int = 3, cooldown: timedelta = timedelta(minutes=10), clock=...)`
    - `.allow(provider: str) -> bool`, `.success(provider)` and `.failure(provider)`.
    - It is open after `threshold` consecutive failures, until `cooldown` elapses.
  - `class ProviderUnavailable(RuntimeError)`.

- [ ] **Step 1: Write failing tests** (MockTransport with a scripted response list; the injected `sleep` records delays)

```python
def test_retries_503_then_succeeds():                      # 503, 503, 200 -> 200; slept [0.5, 1.0]
def test_honors_retry_after_header():                      # 429 Retry-After: 7, then 200 -> slept [7]
def test_does_not_retry_401():                             # raises HTTPStatusError after 1 call
def test_gives_up_after_max_retries():                     # 4x 503 -> HTTPStatusError, 4 calls
def test_min_interval_spaces_requests():                   # two calls with min_interval=1.0 -> one sleep ≈1.0
def test_circuit_opens_after_three_failures_and_recovers():  # allow False after 3 failures; True after cooldown
def test_success_resets_failure_count():
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/sources/test_http.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** with the signatures above.
- [ ] **Step 4: Run** the tests → PASS; then `make typecheck`.
- [ ] **Step 5: Commit:** `feat(sources): retrying HTTP client, Fetched envelope and circuit breaker`

---

### Task 3: Odds API markets, client and parser

**Files:**
- Create: `src/sharpline/sources/rows.py`, `src/sharpline/sources/odds_api/{__init__,markets,client,parse}.py`, `tests/fixtures/odds_api/nba_events.json`, `nba_game_odds.json`, `nba_event_props.json`
- Test: `tests/unit/sources/odds_api/test_markets.py`, `test_client.py`, `test_parse.py`

**Interfaces:**
- Consumes: `HttpClient`, `Fetched`, `QuotaInfo` (Task 2); `MarketType`, `Period`, `Side`, `validate_american` (Phase 0).
- Produces:
  - `rows.py` (frozen pydantic models):
    - `EventRow(provider, provider_event_id, sport, home_team: str, away_team: str, commence_time)`
    - `OddsRow(provider_event_id, market_key, market_type, period, stat: str | None, player_name: str | None, book, side, price: int, line: Decimal | None, captured_at)`
    - `InjuryRow(team_provider_id, player_name, status, detail, reported_at)`
    - `ResultRow(provider_event_id, home_score, away_score, final_at)`
    - `PlayerGameStatRow(provider_game_id, provider_player_id, player_name, team_abbr, stats: dict[str, float])`
  - `markets.py`:
    - `GAME_MARKETS = ("h2h", "spreads", "totals")`
    - `NBA_PROP_MARKETS` = the 6 keys in Global Constraints
    - `describe_market(key: str) -> MarketSpec(market_type, period, stat)`; unknown keys raise `UnsupportedMarket`.
    - Stat names: `points`, `rebounds`, `assists`, `threes`, `points_rebounds_assists`, `blocks`.
    - Suffixes: `_h1` → `H1`, `_q1` → `Q1`.
  - `OddsApiClient(api_key: SecretStr, http: HttpClient, sport_key: str = "basketball_nba")` methods, each returning `Fetched` with `quota` parsed from `x-requests-last/used/remaining`:
    - `.events() -> Fetched`
    - `.game_odds() -> Fetched`
    - `.event_odds(event_id: str, markets: Sequence[str]) -> Fetched`
  - `OddsApiClient.estimate_cost(markets: int, regions: int = 1) -> int` (static) returns `markets * regions`.
  - Pure parsers:
    - `parse_events(payload) -> list[EventRow]`
    - `parse_odds(payload, captured_at: datetime) -> list[OddsRow]` handles both the list (game odds) and single-event (props) shapes.
    - Outcome mapping:
      - h2h: `name` = team → `HOME`/`AWAY`.
      - spreads: `HOME`/`AWAY` with `point`.
      - totals and props: `Over`/`Under` → `OVER`/`UNDER`, with `point`; props take `player_name` from `description`.
      - Yes/No outcomes map to `YES`/`NO`.
    - Outcomes with prices invalid per `validate_american` are dropped and logged.

- [ ] **Step 1: Create fixtures** by hand from The Odds API v4 documented response shapes, trimmed to 2 events and 2 books. They must cover:
  - an h2h/spreads/totals game;
  - a props event where `player_points` lines **differ by book** (DK Over 27.5 −115, FD Over 28.5 +100, for the same player);
  - one outcome with price `50` (invalid).

  The headers fixture is `x-requests-last: 6`, `x-requests-used: 120`, `x-requests-remaining: 380`.
- [ ] **Step 2: Write failing tests**

```python
def test_describe_prop_market():
    assert describe_market("player_points") == MarketSpec(MarketType.PLAYER_PROP, Period.FULL, "points")
def test_describe_first_half_spread():
    assert describe_market("spreads_h1") == MarketSpec(MarketType.SPREAD, Period.H1, None)
def test_unknown_market_raises():  # describe_market("player_foo") -> UnsupportedMarket
def test_client_parses_quota_headers_and_never_sends_key_in_logs(caplog):
    # MockTransport asserts apiKey query param present, regions=us, oddsFormat=american
    f = client.event_odds("evt1", NBA_PROP_MARKETS)
    assert f.quota == QuotaInfo(last=6, used=120, remaining=380)
    assert "sk-test-key" not in caplog.text and "sk-test-key" not in str(f.params)
def test_parse_props_keeps_per_book_lines():
    rows = parse_odds(load("nba_event_props.json"), captured_at=T0)
    tatum = [r for r in rows if r.player_name == "Jayson Tatum" and r.side == Side.OVER]
    assert {(r.book, r.line, r.price) for r in tatum} == {("draftkings", Decimal("27.5"), -115), ("fanduel", Decimal("28.5"), 100)}
def test_parse_game_odds_sides():  # h2h home/away mapped by team name; totals OVER/UNDER with point
def test_invalid_price_dropped():  # the price=50 outcome is absent
```

`Fetched.params` must **exclude** `apiKey`.
- [ ] **Step 3: Run** `uv run pytest tests/unit/sources/odds_api -q`. Expected: FAIL.
- [ ] **Step 4: Implement**.
- [ ] **Step 5: Run** the tests → PASS; then `make typecheck`.
- [ ] **Step 6: Commit:** `feat(odds-api): market mapping, quota-aware client and pure odds parser`

---

### Task 4: ESPN client and parsers

**Files:**
- Create: `src/sharpline/sources/espn/{__init__,client,parse}.py`, `tests/fixtures/espn/nba_scoreboard.json`, `nba_scoreboard_final.json`, `nba_injuries.json`
- Test: `tests/unit/sources/espn/test_parse.py`, `test_client.py`

**Interfaces:**
- Consumes: `HttpClient`, `Fetched`, `EventRow`, `InjuryRow`, `ResultRow`.
- Produces:
  - `EspnClient(http: HttpClient)`, with `http` based at `https://site.api.espn.com/apis/site/v2/sports/basketball/nba`:
    - `.scoreboard(slate_date: date) -> Fetched` (params `{"dates": "YYYYMMDD"}`)
    - `.injuries() -> Fetched`
  - `parse_scoreboard(payload) -> tuple[list[EventRow], list[ResultRow]]`:
    - `EventRow.home_team`/`away_team` = ESPN team **abbreviation**; `provider="espn"`.
    - Results only for `status.type.name == "STATUS_FINAL"`.
  - `parse_injuries(payload) -> list[InjuryRow]`:
    - `team_provider_id` = ESPN team id; `player_name` = `athlete.displayName`; status lowercased.
    - Status mapping: `"Day-To-Day"` → `questionable`; unknown statuses are logged and dropped.

- [ ] **Step 1: Capture fixtures** once from the live ESPN endpoints (free, no key): today's scoreboard, a past date's scoreboard with finals, and injuries. Trim to 2 events and 2 teams, then commit.
- [ ] **Step 2: Write failing tests**

```python
def test_scoreboard_events_use_abbreviations_and_utc(): ...   # commence_time tz == UTC; home_team == "DET" (per fixture)
def test_scoreboard_results_only_for_finals(): ...            # final fixture -> 2 ResultRow; scheduled fixture -> 0
def test_injuries_map_status_and_team(): ...                  # "Out" -> "out"; "Day-To-Day" -> "questionable"
def test_client_formats_date_param(): ...                     # MockTransport sees dates=20261112
```

- [ ] **Step 3: Run** → FAIL. **Step 4: Implement.** **Step 5: Run** → PASS; then `make typecheck`.
- [ ] **Step 6: Commit:** `feat(espn): scoreboard and injuries client with pure parsers`

---

### Task 5: nba_api stats client and parser

**Files:**
- Create: `src/sharpline/sources/nba_stats/{__init__,client,parse}.py`, `tests/fixtures/nba_stats/league_game_log_players.json`
- Test: `tests/unit/sources/nba_stats/test_parse.py`, `test_client.py`

**Interfaces:**
- Consumes: `Fetched`, `PlayerGameStatRow`.
- Produces:
  - `NbaStatsClient(endpoint_factory: Callable[..., Any] = LeagueGameLog, sleep=time.sleep, min_interval: float = 1.0)`
  - `.player_game_logs(season: str, date_from: date | None = None, date_to: date | None = None) -> Fetched` calls `LeagueGameLog(season=..., player_or_team_abbreviation="P", date_from_nullable=..., date_to_nullable=...)` and stores `get_dict()` as the payload.
  - `parse_player_game_logs(payload) -> list[PlayerGameStatRow]` maps result-set headers to stats:
    - `points` ← `PTS`, `rebounds` ← `REB`, `assists` ← `AST`, `threes` ← `FG3M`, `blocks` ← `BLK`, `steals` ← `STL`, `turnovers` ← `TOV`, `minutes` ← `MIN`
    - `points_rebounds_assists` = PTS + REB + AST
    - `provider_game_id` = `GAME_ID`; `provider_player_id` = `PLAYER_ID`; `team_abbr` = `TEAM_ABBREVIATION`.

- [ ] **Step 1: Capture the fixture** with one local call for a past date range, trimmed to 5 rows (the executor runs this; stats.nba.com is reachable from a home IP).
- [ ] **Step 2: Write failing tests**

```python
def test_parse_maps_headers_and_derives_pra(): ...  # first row: stats["points_rebounds_assists"] == PTS+REB+AST
def test_client_passes_season_and_dates_without_network():
    calls = []; client = NbaStatsClient(endpoint_factory=lambda **kw: calls.append(kw) or FakeEndpoint(load(...)), sleep=lambda s: None)
    client.player_game_logs("2025-26", date(2026, 11, 12), date(2026, 11, 12))
    assert calls[0]["date_from_nullable"] == "11/12/2026"
```

- [ ] **Step 3: Run** → FAIL. **Step 4: Implement.** **Step 5: Run** → PASS.
- [ ] **Step 6: Commit:** `feat(nba-stats): league game log client and stat parser`

---

### Task 6: Name normalization, NBA team seed and EntityResolver

**Files:**
- Create: `src/sharpline/ingest/__init__.py`, `ingest/normalize.py`, `ingest/resolve.py`, `src/sharpline/sports/__init__.py`, `sports/nba/__init__.py`, `sports/nba/teams.py`
- Test: `tests/unit/ingest/test_normalize.py`, `tests/unit/sports/test_nba_teams.py`, `tests/integration/ingest/test_resolve.py`

**Interfaces:**
- Produces:
  - `normalize_name(name: str) -> str`: NFKD, accents stripped, lowercased, punctuation removed, suffixes removed (`jr`, `sr`, `ii`, `iii`, `iv`), whitespace collapsed.
  - `NbaTeam(abbr: str, name: str, espn_abbr: str, espn_id: str, odds_api_name: str, nba_api_id: int)`; `NBA_TEAMS: tuple[NbaTeam, ...]` (30 entries).
  - `seed_nba_teams(session) -> int` upserts `teams` plus `entity_xref` for providers `espn` (abbr and id), `odds_api` (full name) and `nba_stats` (id). It is idempotent and returns the number of teams.
  - `Resolution = Resolved(entity_id: int) | NeedsReview(review_id: int)`
  - `EntityResolver(session)` methods:
    - `.team(provider: str, provider_id: str) -> Resolved | NeedsReview`
    - `.player(provider: str, provider_id: str, raw_name: str, *, sport: Sport, team_id: int | None) -> Resolved | NeedsReview`
    - `.resolve_review(review_id: int, entity_id: int) -> None` writes the xref and marks the review resolved.
  - Player rule (spec §5):
    - An xref hit returns it.
    - Otherwise players with equal `normalize_name` in the same sport (and team, when given): exactly one → auto xref; zero or several → `entity_review` upsert with `candidates` = up to 5 `{id, name, score}` from `rapidfuzz.process.extract(..., scorer=token_sort_ratio, score_cutoff=90)`, returned as `NeedsReview`.
  - The canonical `players` row is created **only** from `nba_stats` (the authoritative roster source): `.player(provider="nba_stats", ...)` with no match creates the player and its xref. Other providers never create players.

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.parametrize("raw,expected", [("Nicolás Claxton", "nicolas claxton"), ("Jaren Jackson Jr.", "jaren jackson"),
    ("Gary Trent Jr", "gary trent"), ("Marvin Bagley III", "marvin bagley"), ("De'Aaron Fox", "deaaron fox"), ("  P.J.  Washington ", "pj washington")])
def test_normalize_name(raw, expected): assert normalize_name(raw) == expected

def test_thirty_teams_with_unique_ids():  # len 30; abbr, espn_id, odds_api_name, nba_api_id all unique
@pytest.mark.parametrize("espn,ours", [("GS","GSW"),("NY","NYK"),("SA","SAS"),("NO","NOP"),("UTAH","UTA"),("WSH","WAS"),("LAC","LAC")])
def test_espn_abbreviation_divergences(espn, ours): ...
def test_clippers_names_across_providers():  # LAC: odds_api_name == "Los Angeles Clippers"

# integration
def test_seed_is_idempotent(db_session):  # seed twice -> 30 teams, 120 xrefs
def test_every_team_resolves_exactly_from_each_provider(db_session): ...   # all 30 x 3 providers -> Resolved
def test_unique_name_match_creates_xref(db_session): ...                   # odds_api "Nicolas Claxton" -> Resolved to nba_stats "Nicolás Claxton"
def test_ambiguous_name_goes_to_review_and_is_idempotent(db_session): ...   # two "Marcus Morris"-style duplicates -> NeedsReview; repeat call same review_id
def test_unknown_player_from_odds_api_does_not_create_player(db_session): ...
def test_resolve_review_writes_xref(db_session): ...
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** Build `NBA_TEAMS` from `nba_api.stats.static.teams.get_teams()` (ids, names, abbreviations) plus the live ESPN team list (ids, abbreviations), checked into source as literal data. **Step 4: Run** → PASS.
- [ ] **Step 5: Commit:** `feat(ingest): name normalization, NBA team seed and entity resolver with review queue`

---

### Task 7: Writer, observations and ingest jobs

**Files:**
- Create: `src/sharpline/ingest/writer.py`, `ingest/observations.py`, `ingest/jobs.py`
- Test: `tests/integration/ingest/test_writer.py`, `test_observations.py`, `test_jobs.py`

**Interfaces:**
- Consumes: `record_source` (Phase 0); `Fetched`; rows; `EntityResolver`; the parsers from Tasks 3–5.
- Produces:
  - `record_fetch(session, fetched: Fetched, *, capture_run_id: int | None = None, purpose: str | None = None) -> int` performs `record_source` + a `SourceObservation(observed_at=fetched.fetched_at, purpose=purpose)` + (if `fetched.quota`) a `QuotaLedger(cost=quota.last, …)`, and returns `source_id`. Closing odds fetches pass `purpose="closing"`.
  - `Writer(session, resolver)` methods return `WriteStats(written: int, skipped: int, reviews: int)`:
    - `.events(rows, source_id)` upserts `events` + an event xref per provider.
    - `.odds(rows, source_id, *, is_closing: bool)` creates the `Market` per (event, market_key, period, player, **line**) and inserts an `OddsSnapshot`.
    - `.injuries(rows, source_id)`, `.results(rows, source_id)`, `.player_stats(rows, source_id)`.
  - `latest_source(session, provider, endpoint, params, as_of: datetime) -> Source | None` uses `source_observations` with `observed_at <= as_of` ordered desc.
  - Jobs (each opens its own transaction and returns `WriteStats`):
    - `ingest_schedule(session, espn, slate_date)`
    - `ingest_injuries(session, espn)`
    - `ingest_game_lines(session, odds, *, is_closing=False)`
    - `ingest_props(session, odds, event_id: str, markets, *, is_closing=False)`
    - `ingest_results(session, espn, slate_date)`
    - `ingest_player_stats(session, nba, season, slate_date)`
  - Odds API event ids map to our events through `entity_xref (odds_api, event)`. When missing, they're matched by (home team, away team, `commence_time` within ±2 h) and the xref is created.

- [ ] **Step 1: Write failing tests** (integration; seed teams via `seed_nba_teams`)

```python
def test_record_fetch_writes_source_observation_and_quota(db_session): ...
def test_refetch_same_payload_adds_observation_not_source(db_session): ...           # 1 source, 2 observations
def test_latest_source_handles_a_b_a(db_session):
    # payload A at t1, B at t2, A again at t3 -> latest_source(as_of=t3) is A's row; as_of=t2 is B
def test_writer_odds_keeps_per_book_lines_as_separate_markets(db_session): ...      # Review Focus 5: 2 markets (27.5, 28.5), 2 snapshots
def test_writer_odds_skips_unresolved_player_and_counts_review(db_session): ...
def test_writer_is_idempotent_for_events_and_results(db_session): ...
def test_ingest_game_lines_end_to_end_with_fixture(db_session): ...                  # MockTransport-backed OddsApiClient
def test_closing_flag_persisted_via_observation_purpose(db_session): ...  # ingest_props(is_closing=True) -> observation.purpose == 'closing' and snapshots is_closing
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest tests -q` → PASS.
- [ ] **Step 5: Commit:** `feat(ingest): fetch recording, observations-aware lookups, writer and jobs`

---

### Task 8: QuotaGuard and plan_captures

**Files:**
- Create: `src/sharpline/capture/__init__.py`, `capture/quota.py`, `capture/plan.py`
- Test: `tests/unit/capture/test_quota.py`, `test_plan.py`, `test_plan_properties.py`

**Interfaces:**
- Consumes: `QuotaInfo`; `NBA_PROP_MARKETS`, `GAME_MARKETS`.
- Produces:
  - `QuotaGuard(session_cap: int, floor: int)` methods:
    - `.allows(estimated_cost: int) -> Decision(allowed: bool, reason: str | None)`
    - `.charge(actual: QuotaInfo | None, estimated_cost: int) -> None` charges `actual.last` when present, else the estimate, and updates `remaining`.
    - `.credits_used: int` (property).
  - `SlateGame(provider_event_id: str, commence_time: datetime)`; `Budget(session_cap: int, prop_markets: int = 6)`.
  - `CaptureAction(at: datetime, kind: Literal["schedule","injuries","game_lines","props","closing"], event_ids: tuple[str, ...], est_cost: int, reason: str)` (frozen, ordered by `at`).
  - `CapturePlan(actions: tuple[CaptureAction, ...], dropped: tuple[tuple[CaptureAction, str], ...], est_cost: int)`.
  - `plan_captures(games: Sequence[SlateGame], now: datetime, budget: Budget) -> CapturePlan` follows spec §4.1–4.2 exactly:
    - Closing actions batch games with identical `commence_time`. Cost = 3 + `prop_markets` × games.
    - It raises `ClosingExceedsBudget` if closing alone > cap.
    - Each drop's reason is `"budget"`.

- [ ] **Step 1: Write failing tests**

```python
def test_guard_refuses_over_cap(): ...            # cap 10, used 8, est 3 -> not allowed, reason mentions "session_cap"
def test_guard_respects_floor(): ...              # remaining 22, floor 20, est 6 -> not allowed
def test_guard_charges_actual_cost(): ...         # est 6, actual last=4 -> credits_used == 4
def test_plan_two_games_same_tip_one_closing_batch():
    plan = plan_captures([g("a", T730), g("b", T730)], now=T1000am, budget=Budget(180))
    closing = [a for a in plan.actions if a.kind == "closing"]
    assert len(closing) == 1 and closing[0].at == T730 - timedelta(minutes=3) and closing[0].est_cost == 3 + 6*2
def test_plan_drops_actions_in_the_past(): ...    # now=7:29pm ET, tip 7:30pm: closing (7:27) is past -> plan has no action covering that game
def test_budget_drop_order(): ...                 # small cap: hourly game_lines dropped before T-90 props before start props; closing kept
def test_closing_alone_over_cap_raises(): ...
# test_plan_properties.py (Hypothesis): random 1-15 games, tips 12:00-23:00 ET, now before first tip, cap 40-400
#   - no action.at >= its games' commence_time
#   - every game whose T-3 is after `now` has exactly one closing action covering it
#   - plan.est_cost <= cap (or ClosingExceedsBudget raised)
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit:** `feat(capture): quota guard and pure capture planner with budget fitting`

---

### Task 9: Capture runner and live-mode registration

**Files:**
- Create: `src/sharpline/capture/runner.py`
- Modify: `apps/worker/src/sharpline_worker/app.py`
- Test: `tests/integration/capture/test_runner.py`, `tests/unit/worker/test_live_mode.py`

**Interfaces:**
- Consumes: Tasks 2–8.
- Produces:
  - `CaptureRunner(session_factory, *, espn: EspnClient, odds: OddsApiClient | None, guard: QuotaGuard, breaker: CircuitBreaker, clock: Callable[[], datetime], sleep_until: Callable[[datetime], None], prop_markets: Sequence[str] = NBA_PROP_MARKETS)`
  - `.run(sport: Sport, slate_date: date, *, mode: Literal["manual", "live"] = "manual") -> int` returns `capture_run_id`. It:
    1. creates the `CaptureRun`;
    2. runs `schedule`, then plans from the stored events;
    3. executes actions in time order;
    4. after every `schedule` refresh (hourly), re-reads the slate and re-plans the remaining actions;
    5. persists each action's status in `CaptureRun.plan`;
    6. sets `status` / `finished_at` / `credits_used`.
  - Action rules:
    - `odds is None` → odds actions `skipped("no_api_key")`.
    - Guard refusal → `skipped(<reason>)`.
    - Breaker open → `skipped("circuit_open")`.
    - An exception → `failed(<type>: <msg>)` plus `breaker.failure`, and the run continues.
    - A `closing` action re-reads its games' current `commence_time` and `status` first. If a game moved later, a new closing action is inserted at new T-3. If postponed or already started, that game is dropped from the action with reason `"tip_moved"` / `"started"`.
  - `KeyboardInterrupt` → `status="interrupted"`, re-raised after persisting.
  - Worker: when `settings.live_capture` is true, `build_app` registers a periodic task `plan_and_enqueue` (cron `"*/15 * * * *"`, queue `"capture"`); when false, only the heartbeat.

- [ ] **Step 1: Write failing tests** (fake clock and `sleep_until` that advances it; MockTransport clients)

```python
def test_run_executes_plan_and_records_statuses(db_session_factory): ...
def test_missing_odds_key_skips_odds_but_captures_espn(...): ...                 # Review Focus 1
def test_odds_401_midsession_marks_remaining_failed_and_finishes(...): ...       # Review Focus 4: status completed, ESPN injuries still done
def test_tip_moved_later_reschedules_closing(...): ...                           # Review Focus 3
def test_started_game_never_priced(...): ...                                     # no closing snapshot for a game whose status flips to in-progress
def test_interrupt_marks_run_interrupted(...): ...
# unit
def test_live_mode_off_registers_only_heartbeat(): ...
def test_live_mode_on_registers_capture_job(): ...
```

`db_session_factory` is a new conftest fixture: a `sessionmaker` bound to the test engine, with cleanup that truncates the ingest tables.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest tests -q` → PASS.
- [ ] **Step 5: Commit:** `feat(capture): session runner with re-planning, skips and live-mode worker job`

---

### Task 10: Slate bundles and reparse

**Files:**
- Create: `src/sharpline/bundles/{__init__,format,export,load}.py`, `src/sharpline/ingest/reparse.py`
- Test: `tests/unit/bundles/test_format.py`, `tests/integration/bundles/test_roundtrip.py`

**Interfaces:**
- Consumes: `record_source`, `SourceObservation`, the parsers and `Writer`.
- Produces:
  - `BundleManifest(format_version: Literal[1], sport, slate_date, capture_run_id, alembic_revision, sharpline_version, counts: dict[str, int], content_sha256)`
  - `export_bundle(session, capture_run_id: int, out_dir: Path) -> Path` collects sources linked to the run (via `quota_ledger.capture_run_id` or `source_observations` within `[started_at, finished_at]`) and writes `<sport>-<date>-<sha8>.jsonl.gz`: the manifest line, then `source` and `observation` lines. `content_sha256` covers all lines after the manifest.
  - `load_bundle(session, path: Path) -> LoadReport(sources_inserted, sources_existing, observations, reparsed: WriteStats)` raises `BundleIntegrityError` on a hash mismatch or unknown `format_version`.
  - `PARSERS: dict[tuple[str, str], Callable]` maps (provider, endpoint pattern) → (parser, writer method).
  - `reparse(session, *, provider: str | None = None, source_ids: Iterable[int] | None = None) -> WriteStats` re-derives rows. An odds source with any observation whose `purpose == "closing"` is written with `is_closing=True`. Bundle observation lines carry `purpose`, so the flag survives export → load without `capture_runs`.

- [ ] **Step 1: Write failing tests**

```python
def test_manifest_hash_detects_tampering(tmp_path): ...
def test_unknown_format_version_rejected(tmp_path): ...
def test_roundtrip_export_load_reparse_reproduces_typed_rows(db_session, other_db_session):
    # run a capture against MockTransport fixtures in DB A; export; load into fresh DB B
    # assert snapshot(DB A) == snapshot(DB B) for events, markets, odds_snapshots (incl. is_closing), injuries, results
def test_load_is_idempotent(...): ...                     # second load: sources_inserted == 0
def test_reparse_after_parser_fix_rebuilds_rows(...): ...
```

`other_db_session` is a second throwaway database fixture. `snapshot()` selects natural-key tuples (no surrogate ids).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit:** `feat(bundles): hashed slate bundles, idempotent load and reparse`

---

### Task 11: Bundle store and private bucket

**Files:**
- Create: `src/sharpline/bundles/store.py`
- Modify: `.railway/railway.ts`, `.env.example`
- Test: `tests/unit/bundles/test_store.py`

**Interfaces:**
- Produces:
  - `BundleStore` protocol: `.push(path: Path) -> str`, `.pull(name: str, dest: Path) -> Path`, `.list() -> list[str]`.
  - `LocalBundleStore(root: Path)`.
  - `S3BundleStore(bucket, endpoint_url, region, access_key_id: SecretStr, secret_access_key: SecretStr, client=None)` uses boto3 S3 with key prefix `bundles/`.
  - `store_from_settings(settings) -> BundleStore` returns `S3BundleStore` when `bundle_bucket` is set, else `LocalBundleStore(Path("var/bundles"))`.
  - `.railway/railway.ts` adds `bucket("sharpline-bundles", { region: "sjc" })`. Bucket credentials reach local `.env` only by the owner copying them from the dashboard (`BUNDLE_BUCKET`, `BUNDLE_ENDPOINT_URL`, `BUNDLE_ACCESS_KEY_ID`, `BUNDLE_SECRET_ACCESS_KEY`).

- [ ] **Step 1: Write failing tests:** local push/pull/list round-trip; S3 store using `botocore.stub.Stubber` asserting `put_object` with `Key="bundles/<name>"` and `list_objects_v2` prefix; `store_from_settings` selection.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement**, and add `var/` to `.gitignore`. **Step 4: Run** → PASS.
- [ ] **Step 5: Commit:** `feat(bundles): local and S3 bundle stores; private Railway bucket in IaC`
- [ ] **Step 6 (owner):** `railway config plan` must show exactly `+ bucket sharpline-bundles`. The owner runs `railway config apply` and copies the bucket credentials into `.env`.

---

### Task 12: `sharpline` CLI

**Files:**
- Create: `src/sharpline/cli.py`
- Modify: `pyproject.toml` (`[project.scripts] sharpline = "sharpline.cli:app"`), `Makefile`
- Test: `tests/unit/test_cli.py`, `tests/integration/test_cli_db.py`

**Interfaces:**
- Consumes: everything above.
- Produces: a Typer app with the commands from spec §6.2 (`capture nba`, `results nba`, `backfill nba-stats`, `bundle export|push|pull|load`, `reparse`, `review list|resolve|reject`, `quota`).
  - `capture nba --dry-run` prints a table (time ET, kind, games, est. credits), a total and the dropped actions, and **makes no network calls besides ESPN schedule**. With `--offline`, the schedule is read from the DB instead.
  - `quota` prints the latest `remaining` and the credits used per capture run.

- [ ] **Step 1: Write failing tests** with `typer.testing.CliRunner`:
  - `--help` lists every command;
  - `capture nba --date 2026-11-12 --dry-run --offline` against seeded events prints `est. credits` and `closing`;
  - `review list` shows an open review;
  - `quota` with no ledger prints `no Odds API calls recorded`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest tests -q && make lint typecheck` → PASS.
- [ ] **Step 5: Commit:** `feat(cli): sharpline command line for capture, bundles, review and quota`

---

### Task 13: Capture runbook, first real capture and Phase 1 exit (owner participation)

**Files:**
- Create: `docs/runbooks/capture.md`, `docs/adrs/0004-record-and-replay.md`
- Modify: `README.md` (Phase 1 status + link to the runbook)

**Interfaces:**
- Consumes: the CLI (Task 12) and the bucket (Task 11).

- [ ] **Step 1: Write** the runbook:
  - prerequisites (free Odds API key in `.env`, `seed_nba_teams`, bucket creds);
  - the day-of commands (`--dry-run` first, then `capture nba`);
  - the next-morning commands (`results nba`, `bundle export`, `bundle push`);
  - loading into production (`DATABASE_URL` from Railway, `bundle pull` + `bundle load`);
  - handling the review queue;
  - Ctrl-C behavior.
- [ ] **Step 2: Write** ADR 0004 (record-and-replay operating model; ≤ 30 lines).
- [ ] **Step 3: Run** `sharpline backfill nba-stats --seasons 2023-24 2024-25 2025-26` locally. Expected: the `player_game_stats` count > 70,000 and `review list` empty for `nba_stats`.
- [ ] **Step 4 (owner):** run the first real capture day: `sharpline capture nba --date <game day>`. Expected:
  - the run ends `completed`;
  - every game has a closing snapshot (`select count(distinct m.event_id) from odds_snapshots s join markets m on m.id=s.market_id where s.is_closing` equals the slate's game count);
  - `sharpline quota` shows ≤ 180 credits.
- [ ] **Step 5 (owner):** the next morning, `results nba`, then `bundle export` and `bundle push`. Load into production and verify the same counts there.
- [ ] **Step 6:** Repeat Steps 4–5 on two more game days. Resolve the review queue. The Phase 1 exit criteria in spec §1.2 are met.
- [ ] **Step 7: Commit:** `docs: capture runbook, ADR 0004 and Phase 1 status`

---

## Spec coverage

| Spec section | Task |
|---|---|
| §1.1 operating model, live flag | 1, 9, 13 |
| §1.2 exit criteria | 13 |
| §2 data sources | 3, 4, 5 |
| §3.1–3.2 flow and modules | 2–7, 10, 12 |
| §3.3 schema 0003 | 1 |
| §4.1–4.2 capture planning | 8 |
| §4.3 QuotaGuard | 8, 9 |
| §5 entity resolution | 6, 7 |
| §6.1 bundles | 10, 11 |
| §6.2 CLI | 12 |
| §6.3 live mode | 9 |
| §7 error handling | 2, 9 |
| §8 testing | every task; round trip in 10 |

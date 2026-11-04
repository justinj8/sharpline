# Phase 0 — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A uv-workspace monorepo with tooling, CI, the core provenance/slate/odds schema, domain odds math, a FastAPI `/v1/health` endpoint and a procrastinate worker, running locally against a Railway dev-environment Postgres and deployed to Railway production. No Docker on the developer machine.

**Architecture:** Three Python packages in one uv workspace:
- `sharpline` (core library in `src/sharpline`) holds config, domain types and the database layer.
- `sharpline_api` (`apps/api`) and `sharpline_worker` (`apps/worker`) depend on the core, never the reverse.

Postgres 16 + pgvector is the only stateful service; Alembic owns all DDL, including procrastinate's schema. Railway's Railpack builder produces one image that runs either process. Local development and integration tests use a Railway `dev` environment Postgres; each test session creates and drops its own throwaway database there.

**Tech Stack:** Python 3.12, uv, FastAPI, uvicorn, SQLAlchemy 2 (psycopg 3), Alembic, procrastinate, pydantic v2 + pydantic-settings, structlog, pytest, ruff, mypy, pre-commit, gitleaks, GitHub Actions, Railway (Railpack, CLI 4.x).

**Spec:** `docs/superpowers/specs/2026-11-04-sharpline-design.md` (§3 architecture, §4.1–4.2 data model, §11 operations, §12 CI/CD, §13 phase 0)

## Global Constraints

- Python `>=3.12,<3.13`; dependency management with uv only (no pip/requirements.txt).
- Postgres 16 with the `vector` extension: Railway Postgres (pgvector) in `dev` and `production` environments; CI uses a `pgvector/pgvector:pg18` GitHub Actions service container. No Docker is required on the developer machine.
- Integration tests read `TEST_DATABASE_URL` (an admin URL on the Railway dev Postgres); when it is unset they are skipped with a clear reason, so `make test.unit` works offline.
- All timestamps are `timestamptz` in the DB and timezone-aware UTC in Python; naive datetimes are rejected.
- Secrets come only from environment variables; no key, token or password is ever committed. gitleaks runs in pre-commit and CI.
- `mypy --strict` must pass on `src/` and `apps/*/src/`.
- `sources` rows are append-only: the database itself rejects UPDATE and DELETE.
- API routes live under `/v1`.
- Scheduling timezone is `America/New_York` (used from Phase 1; config key defined now).
- Commit messages follow Conventional Commits and end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Railway hands out `postgres://` / `postgresql://` URLs, but SQLAlchemy with psycopg 3 needs `postgresql+psycopg://`. The URL must be normalized, or the deploy crashes on boot. → test in Task 2.
2. `/v1/health` with an unreachable database must return 503 within ~3 s, not hang the request until the platform health check times out. → test in Task 6.
3. Re-fetching an identical payload must not create a duplicate `sources` row, while the same payload under different params must create a new one. → test in Task 5.
4. American odds between -99 and +99 (e.g. `0`, `50`) are invalid and must raise, not silently produce probabilities > 1. → test in Task 3.
5. Secrets (`SecretStr`) must never appear in `repr(settings)` or in structured logs. → test in Task 2.

---

## File Structure

```
pyproject.toml                      workspace root + core package `sharpline`, tool config
uv.lock
alembic.ini
Makefile
railpack.json                       Railpack build: uv sync in the install step
.env.example
.gitignore
.pre-commit-config.yaml
.gitleaks.toml
.github/workflows/ci.yml
railway.api.json                    Railway config-as-code for the api service
railway.worker.json                 Railway config-as-code for the worker service
README.md
src/sharpline/__init__.py           __version__
src/sharpline/config.py             Settings
src/sharpline/observability/logging.py  configure_logging
src/sharpline/domain/enums.py       Sport, MarketType, Period, Side
src/sharpline/domain/odds.py        odds conversions + de-vig
src/sharpline/domain/time.py        ensure_utc
src/sharpline/db/base.py            DeclarativeBase + naming convention
src/sharpline/db/session.py         normalize_database_url, make_engine, make_session_factory
src/sharpline/db/models/__init__.py re-exports all models
src/sharpline/db/models/provenance.py  Source
src/sharpline/db/models/entities.py    Team, Player, EntityXref
src/sharpline/db/models/slate.py       Event, Market
src/sharpline/db/models/prices.py      OddsSnapshot
src/sharpline/db/sources.py         canonical_hash, record_source
db/migrations/env.py
db/migrations/script.py.mako
db/migrations/versions/0001_core_schema.py
db/migrations/versions/0002_procrastinate_schema.py
apps/api/pyproject.toml
apps/api/src/sharpline_api/__init__.py
apps/api/src/sharpline_api/app.py          create_app
apps/api/src/sharpline_api/middleware.py   RequestIdMiddleware
apps/api/src/sharpline_api/routes/health.py
apps/worker/pyproject.toml
apps/worker/src/sharpline_worker/__init__.py
apps/worker/src/sharpline_worker/app.py    build_app, heartbeat task
apps/worker/src/sharpline_worker/__main__.py
tests/conftest.py                   throwaway-database + migrated engine fixtures
tests/unit/...                      no DB, no network
tests/integration/...               real Postgres via TEST_DATABASE_URL (marker: integration)
```

---

### Task 1: Workspace scaffold and tooling

**Files:**
- Create: `pyproject.toml`, `apps/api/pyproject.toml`, `apps/worker/pyproject.toml`, `src/sharpline/__init__.py`, `apps/api/src/sharpline_api/__init__.py`, `apps/worker/src/sharpline_worker/__init__.py`, `Makefile`, `.gitignore`, `.env.example`, `.pre-commit-config.yaml`, `.gitleaks.toml`, `README.md`
- Test: `tests/unit/test_package.py`

**Interfaces:**
- Produces: importable packages `sharpline`, `sharpline_api`, `sharpline_worker`; `sharpline.__version__: str == "0.1.0"`; Make targets `fmt`, `lint`, `typecheck`, `test`, `test.unit`, `test.integration`, `check` (= lint + typecheck + test), `db.migrate`, `dev`, `worker`. Make loads `.env` when present.

- [ ] **Step 1: Create the root `pyproject.toml`**
  - `[project] name="sharpline" version="0.1.0" requires-python=">=3.12,<3.13"`, src layout, build backend `hatchling`.
  - Runtime deps: `pydantic>=2.9`, `pydantic-settings>=2.5`, `sqlalchemy>=2.0.35`, `psycopg[binary]>=3.2`, `alembic>=1.13`, `structlog>=24.4`.
  - `[tool.uv.workspace] members=["apps/api","apps/worker"]`.
  - Dev dependency group: `pytest`, `pytest-asyncio`, `httpx`, `mypy`, `ruff`, `pre-commit` (plus the two workspace apps so one `uv sync` installs everything).
  - `[tool.ruff]`: line-length 100, target py312, rules `E,F,I,B,UP,SIM,RUF`.
  - `[tool.mypy]`: `strict=true`, `plugins=["pydantic.mypy"]`.
  - `[tool.pytest.ini_options]`: `testpaths=["tests"]`, `markers=["integration: needs a Postgres reachable via TEST_DATABASE_URL"]`, `asyncio_mode="auto"`.
- [ ] **Step 2: Create `apps/api/pyproject.toml`** (`sharpline-api`, deps `sharpline` (workspace source), `fastapi>=0.115`, `uvicorn[standard]>=0.32`) and **`apps/worker/pyproject.toml`** (`sharpline-worker`, deps `sharpline`, `procrastinate>=2.14`), each with `[tool.uv.sources] sharpline = { workspace = true }` and a src layout.
- [ ] **Step 3: Write the failing test** `tests/unit/test_package.py`

```python
import sharpline, sharpline_api, sharpline_worker

def test_packages_import_and_version():
    assert sharpline.__version__ == "0.1.0"
    assert sharpline_api is not None and sharpline_worker is not None
```

- [ ] **Step 4: Run** `uv sync --all-packages && uv run pytest tests/unit/test_package.py -v`. Expected: FAIL (`__version__` missing) until the `__init__.py` files exist. Add them, rerun → PASS.
- [ ] **Step 5: Add support files:**
  - `.gitignore`: Python, `.venv`, `.env`, `.mypy_cache`, `.pytest_cache`, `node_modules`.
  - `.env.example`: empty `DATABASE_URL=` and `TEST_DATABASE_URL=` (comment: copy the Railway dev Postgres public URL), `SHARPLINE_ENV=local`, `LOG_LEVEL=INFO`, empty `ANTHROPIC_API_KEY=` and `ODDS_API_KEY=`.
  - `.pre-commit-config.yaml`: ruff, ruff-format, gitleaks.
  - `.gitleaks.toml`: extend the default config; allowlist `.env.example`.
  - `Makefile` targets listed above; `test.unit` = `pytest -m "not integration"`.
  - `README.md`: one-paragraph description + quickstart (`uv sync --all-packages`, copy the Railway dev URLs into `.env`, `make db.migrate`, `make dev`).
- [ ] **Step 6: Run** `make lint typecheck test.unit`. Expected: all pass.
- [ ] **Step 7: Commit:** `chore: scaffold uv workspace, tooling and pre-commit`

---

### Task 2: Settings and structured logging

**Files:**
- Create: `src/sharpline/config.py`, `src/sharpline/observability/__init__.py`, `src/sharpline/observability/logging.py`, `src/sharpline/db/__init__.py`, `src/sharpline/db/session.py`
- Test: `tests/unit/test_config.py`, `tests/unit/test_logging.py`, `tests/unit/test_session.py`

**Interfaces:**
- Produces:
  - `Settings(BaseSettings)` with fields:
    - `database_url: str` (required, env `DATABASE_URL`)
    - `env: Literal["local","test","staging","production"] = "local"` (env `SHARPLINE_ENV`)
    - `log_level: str = "INFO"`
    - `anthropic_api_key: SecretStr | None = None`
    - `odds_api_key: SecretStr | None = None`
    - `schedule_timezone: str = "America/New_York"`
    - `db_connect_timeout_s: int = 3`
  - `get_settings() -> Settings` (lru-cached).
  - `configure_logging(level: str, json_output: bool) -> None`.
  - `normalize_database_url(url: str) -> str`.
  - `make_engine(settings: Settings) -> Engine` (applies `connect_timeout`, `pool_pre_ping=True`).
  - `make_session_factory(engine: Engine) -> sessionmaker[Session]`.

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_session.py
import pytest
from sharpline.db.session import normalize_database_url

@pytest.mark.parametrize("raw,expected", [
    ("postgres://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
    ("postgresql://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
    ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
])
def test_normalize_database_url(raw, expected):
    assert normalize_database_url(raw) == expected

def test_normalize_rejects_non_postgres():
    with pytest.raises(ValueError):
        normalize_database_url("mysql://u:p@h/d")

# tests/unit/test_config.py
def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h/d")
    monkeypatch.setenv("ODDS_API_KEY", "super-secret-value")
    s = Settings()
    assert s.odds_api_key is not None and s.odds_api_key.get_secret_value() == "super-secret-value"
    assert "super-secret-value" not in repr(s)

def test_settings_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

# tests/unit/test_logging.py
def test_json_logs_do_not_leak_secrets(capsys, monkeypatch):
    configure_logging("INFO", json_output=True)
    structlog.get_logger().info("boot", odds_api_key=SecretStr("abc123"))
    out = capsys.readouterr().out
    assert json.loads(out.strip().splitlines()[-1])["event"] == "boot"
    assert "abc123" not in out
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_session.py tests/unit/test_config.py tests/unit/test_logging.py -v`. Expected: FAIL (modules missing).
- [ ] **Step 3: Implement** `config.py`, `logging.py` and `session.py` with the signatures above.
  - Logging uses structlog with a processor chain ending in `JSONRenderer` when `json_output` is true and `ConsoleRenderer` otherwise.
  - `SecretStr` values render as `"**********"` via pydantic's default `str()`.
  - `normalize_database_url` rewrites only the scheme.
- [ ] **Step 4: Run** the same tests. Expected: PASS. Then `make typecheck` → PASS.
- [ ] **Step 5: Commit:** `feat(core): settings, database URL normalization and structured logging`

---

### Task 3: Domain enums, time and odds math

**Files:**
- Create: `src/sharpline/domain/__init__.py`, `src/sharpline/domain/enums.py`, `src/sharpline/domain/time.py`, `src/sharpline/domain/odds.py`
- Test: `tests/unit/domain/test_odds.py`, `tests/unit/domain/test_time.py`

**Interfaces:**
- Produces:
  - `Sport(StrEnum)`: `NBA="nba"`, `NFL="nfl"`, `MLB="mlb"`.
  - `MarketType(StrEnum)`: `MONEYLINE`, `SPREAD`, `TOTAL`, `PLAYER_PROP`, `SPECIAL` (values lowercase snake_case).
  - `Period(StrEnum)`: `FULL`, `H1`, `Q1`, `F5`, `I1` (values identical to names).
  - `Side(StrEnum)`: `HOME`, `AWAY`, `OVER`, `UNDER`, `YES`, `NO`.
  - `ensure_utc(dt: datetime) -> datetime`: raises `ValueError` on naive, converts aware to UTC.
  - `class InvalidOddsError(ValueError)`.
  - `validate_american(price: int) -> int`.
  - `american_to_decimal(price: int) -> float`.
  - `decimal_to_american(decimal_odds: float) -> int`.
  - `implied_probability(price: int) -> float`.
  - `devig_multiplicative(prices: Sequence[int]) -> list[float]` (each implied probability divided by their sum; requires ≥ 2 prices).

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/domain/test_odds.py
@pytest.mark.parametrize("price,dec,prob", [
    (150, 2.5, 0.4), (-200, 1.5, 2/3), (100, 2.0, 0.5), (-100, 2.0, 0.5), (-110, 1.909091, 0.523810),
])
def test_conversions(price, dec, prob):
    assert american_to_decimal(price) == pytest.approx(dec, abs=1e-6)
    assert implied_probability(price) == pytest.approx(prob, abs=1e-6)

@pytest.mark.parametrize("bad", [0, 50, -99, 99])
def test_invalid_american_odds_raise(bad):
    with pytest.raises(InvalidOddsError):
        implied_probability(bad)

def test_decimal_round_trip():
    assert decimal_to_american(2.5) == 150
    assert decimal_to_american(1.5) == -200
    with pytest.raises(InvalidOddsError):
        decimal_to_american(1.0)

def test_devig_symmetric_market():
    assert devig_multiplicative([-110, -110]) == pytest.approx([0.5, 0.5])

def test_devig_asymmetric_market_sums_to_one():
    probs = devig_multiplicative([-150, 130])
    assert sum(probs) == pytest.approx(1.0)
    assert probs == pytest.approx([0.579832, 0.420168], abs=1e-6)

def test_devig_requires_two_prices():
    with pytest.raises(ValueError):
        devig_multiplicative([-110])

# tests/unit/domain/test_time.py
def test_ensure_utc_rejects_naive():
    with pytest.raises(ValueError):
        ensure_utc(datetime(2026, 11, 4, 12, 0))

def test_ensure_utc_converts_eastern():
    et = datetime(2026, 11, 4, 14, 0, tzinfo=ZoneInfo("America/New_York"))
    assert ensure_utc(et) == datetime(2026, 11, 4, 19, 0, tzinfo=UTC)
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/domain -v`. Expected: FAIL (modules missing).
- [ ] **Step 3: Implement** the four modules with the signatures above.
- [ ] **Step 4: Run** `uv run pytest tests/unit/domain -v && make typecheck`. Expected: PASS.
- [ ] **Step 5: Commit:** `feat(domain): sport/market enums, UTC guard and odds math`

---

### Task 4: Railway project and dev-environment Postgres (requires owner confirmation)

Creates billable cloud resources. The executor must get the owner's explicit OK before creating anything, must use the `use-railway` skill / Railway MCP tools, and must never print connection strings or passwords into chat, logs or commits.

**Files:**
- Create: `.env` (local only, git-ignored; never committed)
- Modify: `README.md` (quickstart points at the Railway dev environment)

**Interfaces:**
- Produces:
  - Railway project `sharpline` with environment `dev` containing one Postgres service built from the pgvector image `pgvector/pgvector:pg18` (Railway pgvector template), with a public TCP proxy.
  - Local `.env` with `DATABASE_URL` = the dev Postgres public URL (database `railway`) and `TEST_DATABASE_URL` = the same server and admin user (the test fixture creates its own databases there).
  - The `production` environment is created in Task 9.

- [ ] **Step 1: Ask the owner** to confirm creating Railway project `sharpline` with a `dev` environment and a pgvector Postgres in their Railway workspace.
- [ ] **Step 2: Provision** the project, `dev` environment and pgvector Postgres; enable its public networking.
- [ ] **Step 3: Write `.env`** from the service's `DATABASE_PUBLIC_URL` variable without echoing it (e.g. `railway variables --json | jq -r ...` redirected straight into the file).
- [ ] **Step 4: Verify** with `uv run python -c` (psycopg connect using `.env`) that:
  - `select current_setting('server_version_num')::int >= 160000` is true;
  - `select count(*) from pg_available_extensions where name = 'vector'` returns `1`.

  Expected: both checks print `True` / `1`.
- [ ] **Step 5: Commit** the README change only: `docs: point local quickstart at the Railway dev environment`

---

### Task 5: Core schema, migrations and source recording

**Files:**
- Create:
  - `alembic.ini`, `db/migrations/env.py`, `db/migrations/script.py.mako`, `db/migrations/versions/0001_core_schema.py`
  - `src/sharpline/db/base.py`, `src/sharpline/db/models/__init__.py`, `provenance.py`, `entities.py`, `slate.py`, `prices.py`
  - `src/sharpline/db/sources.py`
- Test: `tests/conftest.py`, `tests/integration/test_schema.py`, `tests/integration/test_sources.py`, `tests/unit/test_canonical_hash.py`

**Interfaces:**
- Consumes: `Settings`, `make_engine`, `normalize_database_url` (Task 2); `Sport`, `MarketType`, `Period`, `Side`, `ensure_utc` (Task 3); `TEST_DATABASE_URL` / `DATABASE_URL` (Task 4).
- Produces:
  - `Base` (DeclarativeBase with naming convention `ix_%(column_0_label)s`, `uq_%(table_name)s_%(column_0_name)s`, `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s`, `pk_%(table_name)s`).
  - ORM models `Source`, `Team`, `Player`, `EntityXref`, `Event`, `Market`, `OddsSnapshot`.
  - `canonical_hash(obj: Any) -> str` (sha256 hex of `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`).
  - `record_source(session: Session, *, provider: str, endpoint: str, params: Mapping[str, Any], payload: Any, fetched_at: datetime) -> int` (returns the existing id on duplicate).
  - `db/migrations/env.py` uses `config.get_main_option("sqlalchemy.url")` when set (tests), else `get_settings().database_url`, always through `normalize_database_url`.
  - Test fixtures:
    - `pg_url` (session-scoped): reads `TEST_DATABASE_URL` (skips all integration tests with reason `"TEST_DATABASE_URL not set"` when absent), runs `CREATE DATABASE sharpline_test_<8 hex>` over an autocommit admin connection, upgrades it to head through the Alembic command API, yields its normalized URL, and finally runs `DROP DATABASE … WITH (FORCE)`.
    - `db_session` (function-scoped): a session bound to a connection whose outer transaction is rolled back after the test.

**Schema** (all PKs `bigint identity` unless noted; all timestamps `timestamptz`):

| Table | Columns | Constraints |
|---|---|---|
| `sources` | `id`, `provider text`, `endpoint text`, `params jsonb`, `params_hash char(64)`, `content_hash char(64)`, `payload jsonb`, `fetched_at`, `created_at default now()` | unique(`provider`,`endpoint`,`params_hash`,`content_hash`); index(`provider`,`fetched_at`); trigger `sources_immutable` raises on UPDATE/DELETE |
| `teams` | `id`, `sport text`, `name text`, `abbreviation text` | unique(`sport`,`abbreviation`) |
| `players` | `id`, `sport text`, `full_name text`, `team_id → teams null`, `position text null` | index(`sport`,`full_name`) |
| `entity_xref` | `id`, `entity_kind text check in ('team','player','event')`, `entity_id bigint`, `provider text`, `provider_id text` | unique(`provider`,`entity_kind`,`provider_id`) |
| `events` | `id`, `sport text`, `season int`, `home_team_id → teams`, `away_team_id → teams`, `commence_time`, `status text default 'scheduled'` | unique(`sport`,`home_team_id`,`away_team_id`,`commence_time`); index(`sport`,`commence_time`) |
| `markets` | `id`, `event_id → events`, `market_type text`, `period text default 'FULL'`, `provider_key text`, `stat text null`, `player_id → players null`, `line numeric(6,2) null` | unique NULLS NOT DISTINCT(`event_id`,`provider_key`,`period`,`player_id`,`line`) |
| `odds_snapshots` | `id`, `market_id → markets`, `book text`, `side text`, `price int check (price <= -100 or price >= 100)`, `line numeric(6,2) null`, `captured_at`, `source_id → sources not null`, `is_closing bool default false` | index(`market_id`,`book`,`side`,`captured_at desc`) |

Migration `0001` also runs `CREATE EXTENSION IF NOT EXISTS vector`. Enum-valued columns are `text` with CHECK constraints listing the `StrEnum` values (simpler to migrate than PG enums).

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_canonical_hash.py
def test_hash_ignores_key_order():
    assert canonical_hash({"a": 1, "b": [1, 2]}) == canonical_hash({"b": [1, 2], "a": 1})

def test_hash_detects_value_change():
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})

# tests/integration/test_schema.py  (pytestmark = pytest.mark.integration)
def test_all_core_tables_exist(db_session):
    names = set(inspect(db_session.get_bind()).get_table_names())
    assert {"sources","teams","players","entity_xref","events","markets","odds_snapshots"} <= names

def test_vector_extension_installed(db_session):
    assert db_session.execute(text("select 1 from pg_extension where extname='vector'")).scalar() == 1

def test_odds_price_check_rejects_invalid(db_session, seeded_market, seeded_source):
    db_session.add(OddsSnapshot(market_id=seeded_market, book="dk", side="OVER", price=50,
                                captured_at=datetime.now(UTC), source_id=seeded_source))
    with pytest.raises(IntegrityError):
        db_session.flush()

# tests/integration/test_sources.py
def test_record_source_dedupes_identical_payload(db_session):
    kw = dict(provider="odds_api", endpoint="/v4/sports/basketball_nba/odds",
              params={"markets": "h2h"}, payload={"x": 1}, fetched_at=datetime.now(UTC))
    assert record_source(db_session, **kw) == record_source(db_session, **kw)
    assert db_session.scalar(select(func.count()).select_from(Source)) == 1

def test_record_source_new_row_for_different_params(db_session):
    base = dict(provider="odds_api", endpoint="/e", payload={"x": 1}, fetched_at=datetime.now(UTC))
    a = record_source(db_session, params={"markets": "h2h"}, **base)
    b = record_source(db_session, params={"markets": "totals"}, **base)
    assert a != b

def test_sources_are_immutable(db_session):
    sid = record_source(db_session, provider="espn", endpoint="/e", params={}, payload={"y": 1},
                        fetched_at=datetime.now(UTC))
    with pytest.raises(DBAPIError):
        db_session.execute(text("update sources set provider='x' where id=:i"), {"i": sid})

def test_sources_reject_delete(db_session):
    sid = record_source(db_session, provider="espn", endpoint="/e", params={}, payload={"y": 2},
                        fetched_at=datetime.now(UTC))
    with pytest.raises(DBAPIError):
        db_session.execute(text("delete from sources where id=:i"), {"i": sid})

def test_record_source_rejects_naive_datetime(db_session):
    with pytest.raises(ValueError):
        record_source(db_session, provider="espn", endpoint="/e", params={}, payload={},
                      fetched_at=datetime(2026, 11, 4))
```

`seeded_market` / `seeded_source` are fixtures in `tests/conftest.py` that insert two teams, one event, one market and one source and return the ids.

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_canonical_hash.py tests/integration -v`. Expected: FAIL (modules/tables missing).
- [ ] **Step 3: Implement**
  - Models, `alembic.ini` and `env.py` per the Interfaces.
  - Hand-written `0001_core_schema.py` covering the table above, including the immutability trigger function (`raise exception 'sources rows are immutable'`) and `NULLS NOT DISTINCT`.
  - `record_source`: `INSERT … ON CONFLICT (provider, endpoint, params_hash, content_hash) DO NOTHING RETURNING id`, falling back to a SELECT of the existing id.
- [ ] **Step 4: Run** `uv run pytest tests -v && make typecheck`. Expected: PASS, and no `sharpline_test_*` database is left on the dev server afterwards (`select datname from pg_database where datname like 'sharpline_test_%'` → 0 rows). Then `make db.migrate` → `alembic current` shows `0001 (head)` on the dev database.
- [ ] **Step 5: Commit:** `feat(db): core provenance, slate and odds schema with immutable sources`

---

### Task 6: FastAPI app and `/v1/health`

**Files:**
- Create: `apps/api/src/sharpline_api/app.py`, `apps/api/src/sharpline_api/middleware.py`, `apps/api/src/sharpline_api/routes/__init__.py`, `apps/api/src/sharpline_api/routes/health.py`
- Test: `tests/integration/api/test_health.py`, `tests/unit/api/test_request_id.py`, `tests/unit/api/test_health_unreachable.py`

**Interfaces:**
- Consumes: `Settings`, `get_settings`, `make_engine`, `configure_logging`, `sharpline.__version__`; fixture `pg_url` (Task 5).
- Produces:
  - `create_app(settings: Settings | None = None) -> FastAPI`. It stores the engine on `app.state.engine`, calls `configure_logging(settings.log_level, json_output=settings.env != "local")`, and mounts routers under `/v1`.
  - `HealthResponse(BaseModel)`: `status: Literal["ok","degraded"]`, `db: Literal["ok","unreachable"]`, `version: str`.
  - `GET /v1/health` → 200 `{"status":"ok","db":"ok","version":"0.1.0"}`, or 503 `{"status":"degraded","db":"unreachable",...}`.
  - `RequestIdMiddleware` echoes an incoming `X-Request-ID` or generates a uuid4 hex, sets it on the response header, and binds `request_id` in structlog contextvars.

- [ ] **Step 1: Write failing tests**

```python
# tests/integration/api/test_health.py
def test_health_ok(pg_url):
    client = TestClient(create_app(Settings(database_url=pg_url, env="test")))
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok", "version": "0.1.0"}

# tests/unit/api/test_health_unreachable.py
def test_health_degraded_fast_when_db_unreachable():
    s = Settings(database_url="postgresql://u:p@10.255.255.1:5432/x", env="test", db_connect_timeout_s=1)
    client = TestClient(create_app(s))
    t0 = time.monotonic()
    r = client.get("/v1/health")
    assert r.status_code == 503 and r.json()["db"] == "unreachable"
    assert time.monotonic() - t0 < 3

# tests/unit/api/test_request_id.py
def test_request_id_echoed_and_generated():
    app = FastAPI(); app.add_middleware(RequestIdMiddleware)
    @app.get("/x")
    def x() -> dict[str, str]: return {}
    c = TestClient(app)
    assert c.get("/x", headers={"X-Request-ID": "abc"}).headers["X-Request-ID"] == "abc"
    assert len(c.get("/x").headers["X-Request-ID"]) == 32
```

- [ ] **Step 2: Run** `uv run pytest tests/integration/api tests/unit/api -v`. Expected: FAIL.
- [ ] **Step 3: Implement** with the signatures above. The health check runs `SELECT 1` through `app.state.engine` and catches `OperationalError` → 503. `make dev` runs `uvicorn sharpline_api.app:create_app --factory --reload`.
- [ ] **Step 4: Run** the tests → PASS; `make typecheck` → PASS. Then run `make dev` (against the Railway dev DB from `.env`) and `curl -s localhost:8000/v1/health` → `{"status":"ok","db":"ok","version":"0.1.0"}`.
- [ ] **Step 5: Commit:** `feat(api): FastAPI app factory, request ids and /v1/health`

---

### Task 7: procrastinate worker with heartbeat

**Files:**
- Create: `db/migrations/versions/0002_procrastinate_schema.py`, `apps/worker/src/sharpline_worker/app.py`, `apps/worker/src/sharpline_worker/__main__.py`
- Test: `tests/unit/worker/test_heartbeat.py`, `tests/integration/test_procrastinate_schema.py`

**Interfaces:**
- Consumes: `Settings`, `get_settings`, `normalize_database_url`, `configure_logging`; fixture `db_session` (Task 5).
- Produces:
  - `build_app(settings: Settings, connector: BaseConnector | None = None) -> procrastinate.App`. Its default connector is `PsycopgConnector(conninfo=<normalized url with the +psycopg driver suffix removed>)`.
  - Task `heartbeat(timestamp: int) -> str`, registered as periodic `cron="*/5 * * * *"`, `queue="ops"`. It logs `worker.heartbeat` and returns `"ok"`.
  - `python -m sharpline_worker` runs the worker on queues `["ops"]`; `make worker` runs it with `.env` loaded.
  - Migration `0002` applies `procrastinate.schema.SchemaManager.get_schema()` SQL in `upgrade()`; `downgrade()` drops the procrastinate objects.

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/worker/test_heartbeat.py
async def test_heartbeat_runs_with_in_memory_connector():
    connector = procrastinate.testing.InMemoryConnector()
    app = build_app(Settings(database_url="postgresql://u:p@h/d", env="test"), connector=connector)
    async with app.open_async():
        await app.tasks["sharpline_worker.app.heartbeat"].defer_async(timestamp=0)
        await app.run_worker_async(queues=["ops"], wait=False)
    assert [j["status"] for j in connector.jobs.values()] == ["succeeded"]

def test_heartbeat_is_periodic_every_five_minutes():
    app = build_app(Settings(database_url="postgresql://u:p@h/d", env="test"),
                    connector=procrastinate.testing.InMemoryConnector())
    crons = [p.cron for p in app.periodic_registry.periodic_tasks.values()]
    assert "*/5 * * * *" in crons

# tests/integration/test_procrastinate_schema.py
def test_procrastinate_tables_exist(db_session):
    assert "procrastinate_jobs" in inspect(db_session.get_bind()).get_table_names()
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/worker tests/integration/test_procrastinate_schema.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the migration, `build_app` and `__main__`. If `periodic_registry` internals differ in the installed procrastinate version, assert through its public API instead and keep the cron value.
- [ ] **Step 4: Run** `uv run pytest -v && make typecheck` → PASS. Then `make db.migrate` and `make worker`. Expected: a `worker.heartbeat` log line within 5 min (or defer one manually with `uv run procrastinate --app=sharpline_worker.app.app defer sharpline_worker.app.heartbeat '{"timestamp":0}'`).
- [ ] **Step 5: Commit:** `feat(worker): procrastinate app, schema migration and heartbeat task`

---

### Task 8: GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: workflow `ci` on `push` to `main` and on `pull_request`, with jobs:
  - `lint`: `ruff check` and `ruff format --check`.
  - `typecheck`: `mypy`.
  - `test`: `pytest -v` with a `services.postgres` container (`pgvector/pgvector:pg18`, `POSTGRES_PASSWORD=postgres`, health-checked with `pg_isready`) and `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres`, so integration tests run in CI without Docker on any developer machine.
  - `secrets`: `gitleaks/gitleaks-action@v2` with full history checkout.
  - `build`: `uv sync --frozen --no-dev --all-packages`, mirroring the Railpack install step.
  - All Python jobs use `astral-sh/setup-uv` with caching and `uv sync --all-packages --frozen`.

- [ ] **Step 1: Write** `ci.yml` per the interface.
- [ ] **Step 2: Verify locally** with `make check` (same commands CI runs, integration tests against the Railway dev DB). Expected: PASS.
- [ ] **Step 3: Commit:** `ci: lint, typecheck, tests with postgres service, gitleaks and build check`
- [ ] **Step 4: Verify on GitHub** after Task 9 creates the remote: the first push shows all five jobs green.

---

### Task 9: GitHub remote and Railway production deploy (requires owner confirmation)

Outward-facing steps. The executor must get the owner's explicit OK before creating the GitHub repository, pushing, or creating Railway resources, and must use the `use-railway` skill / Railway MCP tools for Railway operations.

**Files:**
- Create: `railpack.json`, `railway.api.json`, `railway.worker.json`, `scripts/smoke_health.sh`, `docs/adrs/0001-modular-monolith.md`, `docs/adrs/0002-postgres-backed-queue.md`, `docs/adrs/0003-railway-without-local-docker.md`
- Modify: `README.md` (deploy section + live health URL + CI badge)

**Interfaces:**
- Produces:
  - `railpack.json`: provider `python`, `packages.python = "3.12"`. The install step runs `pip install uv` then `uv sync --frozen --no-dev --all-packages`, so the venv ships in the image (lesson from Linewatch commit `095da47`).
  - `railway.api.json`: builder `RAILPACK`, `preDeployCommand: ["alembic upgrade head"]`, `healthcheckPath: "/v1/health"`, start command `uvicorn sharpline_api.app:create_app --factory --host 0.0.0.0 --port $PORT` run from the synced venv.
  - `railway.worker.json`: builder `RAILPACK`, start command `python -m sharpline_worker`, no healthcheck.
  - `scripts/smoke_health.sh BASE_URL`: polls `BASE_URL/v1/health` for up to 120 s; exits 0 on `"status":"ok"`, else prints the last response and exits 1.
  - Railway environment `production` in project `sharpline` with:
    - its own pgvector Postgres;
    - services `api` and `worker` linked to the GitHub repo and their config files;
    - `DATABASE_URL` as a reference to the Postgres private URL, and `SHARPLINE_ENV=production`;
    - a generated public domain on `api`.

- [ ] **Step 1: Ask the owner** to confirm the repo name/visibility (`justinj8/sharpline`, public is the default suggestion for a resume) and the production environment resources.
- [ ] **Step 2: Create the remote and push:** `gh repo create`, then `git push -u origin main`. Verify the CI run is green (Task 8 Step 4).
- [ ] **Step 3: Write** the Railpack/Railway config files, the smoke script and the three short ADRs (context, decision, consequences; ≤ 30 lines each). Commit: `chore(deploy): railpack build, railway service configs and ADRs`, then push.
- [ ] **Step 4: Provision on Railway:** the `production` environment, pgvector Postgres, the `api` and `worker` services with their config paths, variables, and a domain for `api`.
- [ ] **Step 5: Verify:**
  - `bash scripts/smoke_health.sh https://<api-domain>` exits 0.
  - Worker logs show `worker.heartbeat` within 5 minutes.
  - The deploy log shows `alembic upgrade head` reaching `0002`.
- [ ] **Step 6: Add** the live health URL and the CI badge to `README.md`. Commit: `docs: add live deployment links`, then push.

---

## Spec coverage (Phase 0)

| Spec item | Task |
|---|---|
| §3.1 repo layout (apps/api, apps/worker, src/sharpline, db/migrations, docs, tests) | 1, 5, 6, 7 (`infra/` not needed: Railway config files live at the root; `apps/web` arrives in Phase 4) |
| §3.2 stack: uv, FastAPI, SQLAlchemy/Alembic, Postgres+pgvector, procrastinate, structlog | 1–7 |
| §4.1 provenance principle (append-only, hashed, deduped sources) | 5 |
| §4.2 core tables (provenance, entities, slate, prices) | 5 (pipeline/eval/cost tables arrive in Phases 2–3) |
| §11.3 structured logs keyed by request/run | 2, 6 |
| §11.4 secrets via env, gitleaks | 1, 2, 4, 8 |
| §12 CI/CD: ruff, mypy, tests, gitleaks, build check, Railway release migrations | 8, 9 |
| §13 Phase 0: `/health` deployed | 6, 9 |

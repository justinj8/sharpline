# Runbook: capture an NBA slate and load it into production

Sharpline runs on **record and replay** ([ADR 0004](../adrs/0004-record-and-replay.md)).
A capture session runs on your laptop against the Railway **dev** database. Its slate
bundle goes to the private bucket. The bundle is then loaded into **production** from
inside the worker container. Live capture stays off (`SHARPLINE_LIVE_CAPTURE` unset).

Every command below runs from the repo root unless it says otherwise. `make cli
ARGS="…"` works too.

## 0. One-time setup

| What | How |
|---|---|
| Dev database at head | `uv run alembic upgrade head` (adds the ingest tables and seeds the 30 NBA teams). |
| Odds API key | Get a free key at the-odds-api.com and put `ODDS_API_KEY=` in `.env` yourself. Never paste it anywhere else. Without it, odds actions are skipped (`no_api_key`) and only ESPN is captured. |
| Bucket credentials | The `BUNDLE_*` variables in `.env`, piped from `railway bucket credentials` (in `.railway/`). If `BUNDLE_BUCKET` is empty, bundles stay in `var/bundles/`. |
| History (players) | `uv run sharpline backfill nba-stats --seasons 2023-24 2024-25 2025-26`. The command is resumable: rerun it after a failure and finished dates are skipped. It also creates the players that prop and injury names resolve to. |
| Railway CLI | Logged in. `.railway/` is linked to project `sharpline`, environment `production`. |

## 1. Credit budget

The free tier gives 500 credits a month, and the guard keeps a floor of 20, which leaves
480 usable. **Capture each of the three Phase 1 days with `--cap 150`** (450 in total).
When the plan has to drop actions, it drops hourly line refreshes and early props first.
Closing lines are never dropped. The cap is counted per **slate day**, so a second session
on the same date only gets what is left of that day's cap.

`uv run sharpline quota` shows the account balance and the credits each run used.

## 2. Game day

1. **Plan first (free):**
   ```bash
   uv run sharpline capture nba --date 2026-11-12 --cap 150 --dry-run
   ```
   This fetches only the ESPN schedule and prints every action with its time (ET), its
   games and its estimated credits, plus the total and anything dropped. It uses no Odds
   API calls and creates no run. Add `--offline` to plan from the events already stored.
   The plan assumes the session starts now, so run the dry run on the morning of the
   capture.
2. **Capture:**
   ```bash
   uv run sharpline capture nba --date 2026-11-12 --cap 150
   ```
   Leave the laptop awake and online until the last closing, which runs 3 minutes before
   the latest tip. The session re-plans after each hourly schedule refresh. If a tip
   moves, the closing moves with it. Postponed or canceled games are dropped. A game is
   never priced after its tip. Short database blips are retried, and only 3 failures in a
   row stop the run.
3. **When it ends**, the CLI prints the run id, the bundle path, and the next steps.
   Don't push this bundle yet: it has no finals or box scores.

**Ctrl-C or SIGTERM** marks the run `interrupted`, exports a partial bundle, and exits
with code 130. Running `capture` again for the same date starts a new run, which only
gets the credits the date has left.

## 3. Next morning

```bash
uv run sharpline results nba --date 2026-11-12   # ESPN finals + nba_stats box scores
uv run sharpline reparse                         # rebuild rows that needed new players
uv run sharpline bundle export <run_id>          # writes var/bundles/nba-2026-11-12-<sha8>.jsonl.gz
uv run sharpline bundle push var/bundles/nba-2026-11-12-<sha8>.jsonl.gz
uv run sharpline bundle list                     # confirm it is in the bucket
```

Check that every game closed. Run this against the dev database, e.g. with `railway
connect` from a dev-linked directory, or with psql on `DATABASE_URL`:

```sql
select count(distinct m.event_id)
from odds_snapshots s join markets m on m.id = s.market_id
where s.is_closing;          -- equals the slate's game count
```

## 4. Load into production

Production Postgres has no public address, so the load runs **inside the worker
container**, over the private network. Pull from the repo root, which is where `.env`
holds the bucket credentials. Then run the `railway ssh` steps from `.railway/`, the
directory linked to production:

```bash
B=nba-2026-11-12-<sha8>.jsonl.gz
uv run sharpline bundle pull "$B"          # from the bucket into var/bundles/
cd .railway
railway ssh --service worker -- sh -c "cat > /tmp/$B" < "../var/bundles/$B"
railway ssh --service worker -- sh -c "sha256sum /tmp/$B"                 # compare with a local `shasum -a 256`
railway ssh --service worker -- .venv/bin/sharpline bundle load "/tmp/$B"
railway ssh --service worker -- .venv/bin/sharpline quota
```

- **First load: compare checksums.** The stdin copy over `railway ssh` hasn't been
  checked yet, so compare the two checksums before loading. If they differ, stop. A
  damaged file is rejected by `bundle load` anyway, before anything is written.
- **Several bundles:** copy them all, then pass them to one `bundle load`. They load
  oldest slate first.
- **Reloading is safe:** loads are idempotent, so a second load inserts nothing.
- `/tmp` in the container is wiped on redeploy. The bucket is the archive.

## 5. Review queue

```bash
uv run sharpline review list                       # open reviews, with fuzzy candidates
uv run sharpline review resolve <review_id> <entity_id>
uv run sharpline review reject <review_id>
uv run sharpline review reject --all --provider espn --kind player   # asks to confirm
```

- **Resolve** when a candidate is clearly the same player or team. This adds an xref, so
  the next capture or `reparse` writes their rows.
- **Reject** a name that has no entity. For example, an injured player who hasn't played
  this season gets no box score, so no player row exists for him.
- Reviews that a later exact match resolves are closed automatically.
- Run the same commands in production through `railway ssh --service worker --
  .venv/bin/sharpline review …`.

## 6. Phase 1 exit (spec §1.2)

Phase 1 is done when all of these hold:

- Three game days are captured, with closings for every game, finals and box scores.
- Their bundles are in the bucket and loaded into production.
- `quota` shows the credits each run used.
- The review queue is empty or fully resolved.
- CI is green.

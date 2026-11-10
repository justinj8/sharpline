# ADR 0004: Record and replay instead of always-on capture

- **Status:** Accepted
- **Date:** 2026-11-10

## Context

Sharpline is a portfolio project, not a betting service. Running the odds scheduler
around the clock would cost money and eat the Odds API free tier: 500 credits a month
covers about three slates. Yet the forecasting and calibration phases need real
captured odds, including closing lines.

## Decision

- **Capture is manual.** A capture is a one-day session run from a laptop:
  `sharpline capture nba --date D --cap 150`. Its results are added the next morning.
- **Every session becomes a slate bundle.** The bundle (`<sport>-<date>-<sha8>.jsonl.gz`)
  is hashed and deterministic, and it holds the raw sources, their observations and the
  players they reference. Bundles are pushed to a private Railway bucket.
- **Production is built by replay.** Production is populated only by loading bundles,
  from inside the worker over the private network. `bundle load` verifies the hash,
  inserts idempotently, and reparses the typed rows.
- **Live mode ships but stays off.** It is in the code (`SHARPLINE_LIVE_CAPTURE`) and
  disabled by default.
- **CI uses fixtures only.** CI runs on trimmed fixtures, with no network access and no
  keys.

## Consequences

- Rows in production are reproducible from the bucket alone. A parser fix ships with a
  `reparse` instead of a re-fetch.
- Coverage is a few hand-picked days, not a season. The models in later phases must
  treat it as a sample.
- The owner's laptop has to stay awake and online through the last closing of a session.
- Turning live mode on later needs a per-day retry cap, a start-hour gate and a
  single-worker guarantee (see the deferred items in PR #3).

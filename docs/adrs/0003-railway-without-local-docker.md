# ADR 0003: Railway everywhere, no local Docker

- **Status:** Accepted
- **Date:** 2026-11-04

## Context

The original plan used Docker locally (docker-compose Postgres, testcontainers) and a
Dockerfile for deploys. The owner prefers not to run Docker Desktop, and Linewatch
already deploys successfully with Railway's Railpack builder.

## Decision

- **Local dev and integration tests** use a Railway `dev` environment Postgres 18
  (pgvector) through a TCP proxy. The test fixture creates and drops a
  `sharpline_test_<hex>` database per session; without `TEST_DATABASE_URL`, integration
  tests are skipped and unit tests still run offline.
- **CI** uses a `pgvector/pgvector:pg18` GitHub Actions service container.
- **Deploys** use Railpack (`railpack.json`), building the venv in the *install* step
  (the lesson from Linewatch commit `095da47`), with per-service `railway.*.json` files.

## Consequences

- No Docker daemon is needed on the developer machine.
- Integration tests need internet access and add ~10 s of network latency per run.
- The production image is only reproducible on Railway (no local `docker build`); CI's
  `build` job checks that the production `uv sync` resolves from the lockfile.
- Switching back to testcontainers would mean changing a single fixture (`pg_url`).

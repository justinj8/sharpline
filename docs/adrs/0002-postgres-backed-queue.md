# ADR 0002: Postgres-backed job queue (procrastinate)

- **Status:** Accepted
- **Date:** 2026-11-04

## Context

The worker runs periodic ingestion, pipeline runs, grading and email delivery. The usual
choices are Redis-based queues (Celery, RQ, arq) or a queue stored in the existing
Postgres.

## Decision

Use procrastinate, which stores jobs in Postgres. Its schema is applied by Alembic
migration 0002 from SQL vendored and pinned to the procrastinate version it came from.
Alembic autogenerate ignores `procrastinate_*` objects via a shared `include_object`
filter.

## Consequences

- There's no Redis to run, pay for or monitor: Postgres is the only stateful service.
- A job and the rows it writes can commit in the same transaction, which removes the
  "job succeeded but the write was lost" class of bugs.
- Throughput is bounded by Postgres; at a few thousand jobs per day that's far beyond
  what we need.
- Upgrading procrastinate requires a deliberate migration built from its shipped
  migration SQL; the schema never changes silently.

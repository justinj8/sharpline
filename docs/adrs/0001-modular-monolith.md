# ADR 0001: Modular monolith with a sport-plugin interface

- **Status:** Accepted
- **Date:** 2026-11-04

## Context

Sharpline runs one forecasting pipeline for three sports (NBA, NFL, MLB) with a single
operator and a ~$100/month budget. The expensive, slow part of a run is LLM latency,
not CPU. Alternatives considered: a service per sport behind a message bus, and a
monolith orchestrated by a durable workflow engine (Temporal or Prefect).

## Decision

One Python codebase (a uv workspace) deployed as two processes from one image: a FastAPI
`api` and a procrastinate `worker`. Sport-specific logic sits behind a single
`SportModule` protocol (markets, ingestion jobs, features, quant model, research prompts,
card sections, parlay policy, settlement). LangGraph's Postgres checkpointer gives
resumable runs without a second orchestration engine.

## Consequences

- Adding a sport means implementing one interface; there's no new deployable.
- One database and one deploy pipeline keep infrastructure at roughly $15–20/month.
- All sports share a worker, so scaling is horizontal (more worker replicas), not per sport.
- Boundaries are enforced by package layout (`apps/*` depend on `sharpline`, never the
  reverse) rather than by network calls.

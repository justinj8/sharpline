/**
 * Sharpline production infrastructure (Railway IaC).
 *
 *   railway config plan    # preview (read-only)
 *   railway config apply   # apply after reviewing the plan
 *
 * This file owns the `production` environment: a resource omitted here is deleted on
 * apply. The `dev` environment (one Postgres used for local development and integration
 * tests) is intentionally NOT managed by this file, so the guard below refuses to plan
 * against anything but production.
 *
 * Secrets never live here: DATABASE_URL is a reference to the Postgres service, and the
 * database's own credentials stay on Railway.
 */
import { defineRailway, github, postgres, project, service, volume } from "railway/iac";

const REPO = "justinj8/sharpline";

export default defineRailway((ctx) => {
  if (ctx.environment !== "production") {
    throw new Error(
      `.railway/railway.ts manages only "production", not "${ctx.environment}". ` +
        "Run `railway environment production` first.",
    );
  }

  // Postgres 18 (official template, pgvector available). The name is Railway's
  // auto-suffixed one; renaming a database service is not worth the risk.
  const db = postgres("Postgres-c3hF", { region: "sfo" });
  db.networking = { privateNetworkEndpoint: "postgres-c3hf" };
  const dbVolume = volume("postgres-volume-K-S9", {
    alerts: { usage: { "100": {}, "80": {}, "95": {} } },
    allowOnlineResize: true,
    region: "sfo",
    sizeMB: 5000,
  });

  const env = {
    DATABASE_URL: db.env.DATABASE_URL,
    SHARPLINE_ENV: "production",
    LOG_LEVEL: "INFO",
  };

  // Railpack builds the uv workspace venv in its install step (see railpack.json),
  // so the start commands call the venv binaries directly.
  const api = service("api", {
    source: github(REPO, { branch: "main" }),
    build: { buildEnvironment: "V3", builder: "RAILPACK" },
    preDeploy: ".venv/bin/alembic upgrade head",
    start: "sh -c '.venv/bin/uvicorn sharpline_api.app:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}'",
    healthcheck: "/v1/health",
    healthcheckTimeout: 120,
    replicas: { sfo: 1 },
    env,
  });

  const worker = service("worker", {
    source: github(REPO, { branch: "main" }),
    build: { buildEnvironment: "V3", builder: "RAILPACK" },
    start: ".venv/bin/python -m sharpline_worker",
    replicas: { sfo: 1 },
    env,
  });

  return project("sharpline", {
    variables: { managed: true },
    resources: [api, worker, db, dbVolume],
  });
});

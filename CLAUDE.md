# DealSeaking — notes for Claude Code

US electronics deal-alert portal. Design lives in `docs/` (Chinese prose, English identifiers). Read before starting a task:
`docs/design.md` (spec), `docs/decisions.md` (why), `docs/tasks.md` (task briefs), and for collector work `docs/collector-protocol.md`.
Deadline: code freeze 2026-11-23, public launch 2026-11-25 (`docs/plan-black-friday.md`). Prefer the smallest thing that meets a task's acceptance criteria.

## Layout (fixed — parallel tasks rely on it; don't move packages)

```
dealseaking/
  api/        FastAPI routers (JSON API, collector API)
  web/        server-rendered pages: Jinja templates + htmx, static assets
  db/         SQLAlchemy models, session, Alembic env (migrations in db/migrations/)
  sources/    one module per source: parse/fetch → Offer objects. Pure where possible.
  pipeline/   normalize, diff → deal events, scheduling of source jobs
  collector/  server side of the user-collector protocol (leases, uploads, trust)
  rules/      rule schema validation, compiler, inverted index, matcher (pure)
  notify/     outbox, digests, email (Resend), unsubscribe, suppression
extension/    Chrome MV3 collector (plain JS, no bundler unless needed)
collector_cli/  Python/Docker collector client (added by its task)
legacy/openbox-monitor/  the original tool — reference only, never import from it
tests/        mirrors the package: tests/sources/, tests/rules/, …
docs/
```

## Conventions

- Python 3.12, FastAPI, SQLAlchemy 2 (typed `Mapped[...]`), Alembic, Postgres 16. No Redis: background work = APScheduler jobs + Postgres tables (outbox, leases) using `SELECT … FOR UPDATE SKIP LOCKED`.
- Config only via environment (`pydantic-settings`, prefix `DS_`). Never commit secrets; `.env.example` lists every variable.
- `docs/schema.sql` is the reference schema; Alembic migrations must match it. If a task needs a schema change, change both in the same PR.
- Rules are JSON validated against `docs/rule.schema.json`.
- Tests: `pytest`. Pure modules get unit tests with fixtures; DB tests use `DS_DATABASE_URL` pointing at a throwaway Postgres (docker compose service `db-test`). No test may hit a real retailer site.
- Lint: `ruff check .` must pass.
- Dependencies: add to `pyproject.toml` in your PR, one per line, sorted.

## Hard rules (from decisions #5 / #6)

- Never add anti-bot circumvention: no CAPTCHA solving, no fingerprint rotation, no proxies, no headless-browser evasion. On 403 / challenge pages: stop and back off.
- Collector fetches never send the user's Micro Center cookies (`credentials: 'omit'` in the extension).
- Best Buy API data: cache at most 72 hours, show attribution, link back to Best Buy.
- Store clean canonical URLs; affiliate parameters are added only in the `/go/<offer_id>` redirect.
- Every commercial email: one-click unsubscribe (RFC 8058 headers + body link), postal address, affiliate disclosure when links are affiliate links.
- Don't commit real retailer pages that contain a user's IP or visitor IDs; trim fixtures to the product data.

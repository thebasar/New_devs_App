# Property Revenue Dashboard — Debugging Submission

This is a debugging exercise, not a rebuild. I investigated the two client
complaints and the finance team's note, found **five defects**, and fixed each in
its own commit.

**➡️ Full write-up with root causes and before/after evidence: [FINDINGS.md](./FINDINGS.md)**

## The five defects

| # | Symptom | Root cause |
|---|---------|-----------|
| 1 | Client B sees another company's revenue | Redis cache key was `revenue:{property_id}` — property IDs are only unique *within* a tenant, so two tenants sharing `prop-001` shared one cache slot |
| 2 | March totals don't match the client's books | Month boundaries built in UTC, not the property's timezone — a 29 Feb 23:30 UTC booking in Paris belongs to March, not February |
| 3 | Totals off by a few cents | `Decimal` money cast to `float`, then rounded in the UI — the third decimal of `NUMERIC(10,3)` was lost |
| 4 | *(underlying)* dashboard never read the database | Connection pool built its URL from settings that don't exist; every query silently fell back to **hard-coded** revenue |
| 5 | *(latent)* tenants could collapse together | An unresolved tenant fell back to a shared `"default_tenant"` scope instead of being rejected |

## Run it

```bash
docker compose up --build
# Frontend: http://localhost:3000
# Backend API: http://localhost:8000/docs
```

## Run the tests

```bash
docker compose exec backend python -m pytest tests/ -q
```

13 regression tests, no database or Redis server required — the timezone logic is a
pure function and the cache path runs against an in-memory double.

## Verification

Same property ID, two clients, correctly isolated real data:

```
Sunset (tenant-a) -> prop-001 : total_revenue 2250.000, 4 bookings
Ocean  (tenant-b) -> prop-001 : total_revenue 0.00,     0 bookings
```

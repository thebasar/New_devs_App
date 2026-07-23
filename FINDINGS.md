# Findings

Five defects, in the order I would escalate them. The first three are the ones the
clients felt; the fourth is the one I would page someone about tonight.

## 1. Cross-tenant revenue exposure via an unscoped cache key

**Reported as:** *"Sometimes when we refresh the page, we see revenue numbers that
look like they belong to another company."* (Ocean Rentals)

`backend/app/services/cache.py` keyed the Redis entry on the property ID alone:

```python
cache_key = f"revenue:{property_id}"
```

Property IDs are only unique *within* a tenant — `database/schema.sql` gives
`properties` the composite `PRIMARY KEY (id, tenant_id)`, and the seed data relies
on it: `prop-001` is *Beach House Alpha* for `tenant-a` and *Mountain Lodge Beta*
for `tenant-b`. Both tenants therefore addressed one cache slot. Whichever request
populated it first served its revenue to the other tenant for the full 300-second
TTL, which is exactly the intermittent, refresh-dependent behaviour reported.

**Fix:** the key carries the full identity of the row it caches,
`revenue:{tenant_id}:{property_id}`, and building a key without a tenant raises
rather than silently sharing a namespace.

## 2. Monthly revenue attributed in UTC instead of the property's timezone

**Reported as:** *"We're showing different totals for March."* (Sunset Properties)

`calculate_monthly_revenue` built its window with naive datetimes:

```python
start_date = datetime(year, month, 1)
```

`reservations.check_in_date` is `TIMESTAMPTZ` — an absolute instant — so the
comparison happened in UTC. Reservation `res-tz-1` begins at `2024-02-29 23:30 UTC`,
but the property is in `Europe/Paris`, where that instant is already
`2024-03-01 00:30`. The client books, invoices and reports in local time, so the
booking belongs to March in every sense that matters to them.

Measured against the seeded data for `prop-001` / `tenant-a`:

| March window | Total |
|---|---|
| Naive UTC (before) | `1000.000` |
| Property-local, Europe/Paris (after) | `2250.000` |

**Fix:** `month_bounds_utc()` anchors the month in the property's own timezone
(read from `properties.timezone`) and converts to UTC only for the query.

## 3. Money loses precision between the database and the screen

**Reported as:** *"Revenue totals that seem slightly off by a few cents."*

`total_amount` is `NUMERIC(10, 3)` and the service sums it as `Decimal`. Two places
then degraded it:

- `dashboard.py` cast the total to `float`, re-encoding an exact base-10 amount as
  an IEEE-754 binary double. `float('1080.40')` is really
  `1080.400000000000090949...`. Each error is far below a cent, but it accumulates
  across sums and makes totals fail to compare equal to the client's own ledger.
- `RevenueSummary.tsx` then applied `Math.round(total * 100) / 100`, which discards
  the third decimal outright — a `333.333` component silently becomes `333.33`.

**Fix:** the API serialises the `Decimal` as a string, and the component formats it
for display with integer (`BigInt`) arithmetic, so no stored value is altered in
transit or on screen. The rounding indicator now reflects real rounding rather than
float noise.

## 4. The dashboard never read the database at all

This one is not in the brief, and it is the most serious.

`DatabasePool.initialize()` assembled its URL from `settings.supabase_db_user`,
`settings.supabase_db_host` and friends. `Settings` (`backend/app/config.py`)
defines none of them — it defines `database_url`. Every call therefore raised
`AttributeError`, a bare `except` swallowed it, and `session_factory` was left as
`None`.

`calculate_total_revenue` caught the resulting failure and returned this:

```python
mock_data = {
    'prop-001': {'total': '1000.00', 'count': 3},
    'prop-002': {'total': '4975.50', 'count': 4},
    ...
}
```

So the dashboard served hard-coded constants, indistinguishable from real data in
the UI, for every property, always. Those are the numbers the clients were holding
against their own books.

**Fix:**
- Derive the async URL from `settings.database_url`.
- Drop `poolclass=QueuePool` — `create_async_engine` rejects the synchronous pool
  and provides `AsyncAdaptedQueuePool` itself.
- Let initialization failures propagate instead of leaving a dead pool in place;
  `initialize()` is idempotent.
- `get_session()` is an async context manager that always closes the session.
- Remove the fabricated-data fallback. A finance surface must fail loudly rather
  than invent plausible figures.
- Use the shared `db_pool` instead of constructing a new engine per request.

## 5. Unresolved tenants collapsed into a shared scope

`dashboard.py` read the tenant as:

```python
tenant_id = getattr(current_user, "tenant_id", "default_tenant") or "default_tenant"
```

`AuthenticatedUser.tenant_id` is `Optional[str]`. Any user whose tenant failed to
resolve was silently reassigned to a single shared `"default_tenant"` scope —
sharing both the query filter and, with fix 1 in place, the cache namespace.

**Fix:** a request with no resolved tenant is rejected with `403` rather than
reassigned. The parameter is also typed `AuthenticatedUser` instead of `dict`,
which is what `authenticate_request` actually returns; `getattr` on the wrong type
is what made the unsafe default look harmless.

## Verification

With the stack running (`docker compose up --build`):

```
Sunset (tenant-a) -> prop-001 : {"total_revenue":"2250.000","reservations_count":4}
Ocean  (tenant-b) -> prop-001 : {"total_revenue":"0.00","reservations_count":0}
```

Both figures match the seeded rows queried directly in Postgres. The same property
ID now returns each tenant's own revenue, read from the database, as an exact
decimal.

## What I would do next, with more time

- Regression tests: a cache test asserting two tenants with a shared property ID
  never read each other's entry, and a timezone test pinning `res-tz-1` to March
  for a Paris property and to February for a UTC one.
- `calculate_total_revenue` is unfiltered by date while `calculate_monthly_revenue`
  is timezone-correct; the two should share one windowing helper.
- Enforce tenant isolation in the database as well. `schema.sql` enables row level
  security on `properties` and `reservations` but defines no policies, so isolation
  currently rests entirely on application code remembering to filter.
- Cache invalidation on write — the 300-second TTL is the only thing expiring
  stale revenue today.

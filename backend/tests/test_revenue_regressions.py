"""
Regression tests for the five defects fixed in this branch.

These pin the behaviour that the clients actually complained about, so the same
bugs cannot come back silently. They use no database or Redis server: the
timezone logic is a pure function, and the cache path is exercised against an
in-memory double that records the keys it is asked for.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.services.reservations import month_bounds_utc


# --------------------------------------------------------------------------- #
# Defect 2 - monthly revenue must be attributed in the property's local calendar
# --------------------------------------------------------------------------- #

class TestMonthBoundsAreLocalToTheProperty:
    def test_paris_month_starts_before_utc_midnight(self):
        """
        A Paris property's March begins at 2024-02-29 23:00 UTC, because Paris is
        UTC+1 in winter. The naive implementation started March at 00:00 UTC and
        so lost every booking made in that first local hour.
        """
        start, end = month_bounds_utc(2024, 3, "Europe/Paris")

        assert start == datetime(2024, 2, 29, 23, 0, tzinfo=timezone.utc)
        assert end == datetime(2024, 3, 31, 22, 0, tzinfo=timezone.utc)

    def test_seeded_reservation_falls_in_march_for_a_paris_property(self):
        """
        res-tz-1 checks in at 2024-02-29 23:30 UTC - i.e. 2024-03-01 00:30 in
        Paris. It belongs to March for the client that books in Paris local time.
        """
        check_in = datetime(2024, 2, 29, 23, 30, tzinfo=timezone.utc)

        march_start, march_end = month_bounds_utc(2024, 3, "Europe/Paris")
        assert march_start <= check_in < march_end

        feb_start, feb_end = month_bounds_utc(2024, 2, "Europe/Paris")
        assert not (feb_start <= check_in < feb_end)

    def test_same_instant_is_february_under_utc(self):
        """The same booking is February for a UTC property - the bug was
        applying that answer to every property regardless of location."""
        check_in = datetime(2024, 2, 29, 23, 30, tzinfo=timezone.utc)

        feb_start, feb_end = month_bounds_utc(2024, 2, "UTC")
        assert feb_start <= check_in < feb_end

    def test_new_york_month_starts_later_than_utc(self):
        start, _ = month_bounds_utc(2024, 3, "America/New_York")
        assert start == datetime(2024, 3, 1, 5, 0, tzinfo=timezone.utc)

    def test_december_rolls_into_the_next_year(self):
        start, end = month_bounds_utc(2024, 12, "Europe/Paris")
        assert start == datetime(2024, 11, 30, 23, 0, tzinfo=timezone.utc)
        assert end == datetime(2024, 12, 31, 23, 0, tzinfo=timezone.utc)

    def test_window_is_half_open(self):
        """[start, end) - a booking exactly at `end` belongs to the next month."""
        _, march_end = month_bounds_utc(2024, 3, "Europe/Paris")
        april_start, _ = month_bounds_utc(2024, 4, "Europe/Paris")
        assert march_end == april_start

    def test_window_survives_a_dst_transition(self):
        """Europe/Paris springs forward on 2024-03-31; March is 31 days minus the
        lost hour, and the boundaries must still line up with April."""
        start, end = month_bounds_utc(2024, 3, "Europe/Paris")
        assert (end - start).total_seconds() == 31 * 24 * 3600 - 3600


# --------------------------------------------------------------------------- #
# Defect 1 - the revenue cache must be scoped per tenant
# --------------------------------------------------------------------------- #

class RecordingRedis:
    """Minimal stand-in for redis.asyncio.Redis that records the keys used."""

    def __init__(self):
        self.store = {}
        self.requested_keys = []

    async def get(self, key):
        self.requested_keys.append(key)
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value


@pytest.fixture
def fake_redis(monkeypatch):
    from app.services import cache as cache_module

    fake = RecordingRedis()
    monkeypatch.setattr(cache_module, "redis_client", fake)
    return fake


@pytest.fixture
def stub_revenue(monkeypatch):
    """Replaces the DB-backed calculation with a per-tenant constant."""
    from app.services import reservations as reservations_module

    calls = []

    async def _fake_calculate(property_id, tenant_id):
        calls.append((property_id, tenant_id))
        totals = {"tenant-a": "2250.000", "tenant-b": "0.00"}
        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "total": totals.get(tenant_id, "0.00"),
            "currency": "USD",
            "count": 4 if tenant_id == "tenant-a" else 0,
        }

    monkeypatch.setattr(reservations_module, "calculate_total_revenue", _fake_calculate)
    return calls


class TestRevenueCacheIsTenantScoped:
    @pytest.mark.asyncio
    async def test_two_tenants_sharing_a_property_id_do_not_share_a_cache_entry(
        self, fake_redis, stub_revenue
    ):
        """
        'prop-001' exists for both tenant-a and tenant-b in the seed data. Before
        the fix both resolved to the key 'revenue:prop-001', so the second tenant
        read the first tenant's revenue straight out of Redis.
        """
        from app.services.cache import get_revenue_summary

        first = await get_revenue_summary("prop-001", "tenant-a")
        second = await get_revenue_summary("prop-001", "tenant-b")

        assert first["total"] == "2250.000"
        assert second["total"] == "0.00", "tenant-b read tenant-a's cached revenue"

        # Both tenants had to compute their own value; neither was served a hit.
        assert stub_revenue == [("prop-001", "tenant-a"), ("prop-001", "tenant-b")]

    @pytest.mark.asyncio
    async def test_cache_key_carries_the_tenant(self, fake_redis, stub_revenue):
        from app.services.cache import get_revenue_summary

        await get_revenue_summary("prop-001", "tenant-a")

        assert fake_redis.requested_keys == ["revenue:tenant-a:prop-001"]
        assert "revenue:tenant-a:prop-001" in fake_redis.store

    @pytest.mark.asyncio
    async def test_same_tenant_is_served_from_cache(self, fake_redis, stub_revenue):
        """The fix must not defeat caching - a repeat read still hits Redis."""
        from app.services.cache import get_revenue_summary

        await get_revenue_summary("prop-001", "tenant-a")
        await get_revenue_summary("prop-001", "tenant-a")

        assert len(stub_revenue) == 1, "second read should have been a cache hit"

    @pytest.mark.asyncio
    async def test_a_missing_tenant_is_refused(self, fake_redis, stub_revenue):
        """Never fall back to an unscoped key."""
        from app.services.cache import get_revenue_summary

        with pytest.raises(ValueError):
            await get_revenue_summary("prop-001", "")

        assert fake_redis.requested_keys == []


# --------------------------------------------------------------------------- #
# Defect 3 - money must survive the round trip exactly
# --------------------------------------------------------------------------- #

class TestMoneyKeepsItsPrecision:
    @pytest.mark.asyncio
    async def test_cached_total_is_still_an_exact_decimal_string(
        self, fake_redis, stub_revenue
    ):
        """
        The value is JSON-encoded into Redis and decoded again. Encoding it as a
        JSON number would route it through a double on the way back.
        """
        from app.services.cache import get_revenue_summary

        await get_revenue_summary("prop-001", "tenant-a")
        raw = json.loads(fake_redis.store["revenue:tenant-a:prop-001"])

        assert raw["total"] == "2250.000"
        assert isinstance(raw["total"], str)

        from_cache = await get_revenue_summary("prop-001", "tenant-a")
        assert from_cache["total"] == "2250.000"
        assert Decimal(from_cache["total"]) == Decimal("2250.000")

    def test_the_third_decimal_is_not_representable_as_a_float(self):
        """
        Why the total is carried as a string: NUMERIC(10,3) amounts are base-10,
        and doubles are base-2. The stored value and the float are not the same
        number, even where they happen to print alike.
        """
        exact = Decimal("1080.40")
        assert Decimal(float(exact)) != exact
        assert str(exact) == "1080.40"

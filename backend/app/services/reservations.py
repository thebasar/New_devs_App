import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, List
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "UTC"


async def _get_property_timezone(property_id: str, tenant_id: str, session) -> str:
    """Reads the IANA timezone recorded for a property (properties.timezone)."""
    from sqlalchemy import text

    result = await session.execute(
        text(
            "SELECT timezone FROM properties "
            "WHERE id = :property_id AND tenant_id = :tenant_id"
        ),
        {"property_id": property_id, "tenant_id": tenant_id},
    )
    row = result.fetchone()
    return (row.timezone if row and row.timezone else DEFAULT_TIMEZONE)


def month_bounds_utc(year: int, month: int, tz_name: str) -> tuple:
    """
    Returns the [start, end) UTC instants of a calendar month *as observed at the
    property*.

    check_in_date is stored as TIMESTAMPTZ, i.e. an absolute instant. Building the
    month boundary with a naive datetime made Postgres compare it in UTC, so a
    stay that begins on 1 March 00:30 in Paris - 29 Feb 23:30 UTC - was counted
    in February. The client books, invoices and reports in local time, so the
    month window has to be anchored in the property's timezone and only then
    converted to UTC for the query.
    """
    tz = ZoneInfo(tz_name)
    start_local = datetime(year, month, 1, tzinfo=tz)
    if month < 12:
        end_local = datetime(year, month + 1, 1, tzinfo=tz)
    else:
        end_local = datetime(year + 1, 1, 1, tzinfo=tz)

    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


async def calculate_monthly_revenue(
    property_id: str, tenant_id: str, month: int, year: int, db_session=None
) -> Decimal:
    """
    Calculates revenue for a specific month, in the property's local calendar.
    """
    from sqlalchemy import text

    from app.core.database_pool import DatabasePool

    async def _run(session):
        tz_name = await _get_property_timezone(property_id, tenant_id, session)
        start_utc, end_utc = month_bounds_utc(year, month, tz_name)

        logger.debug(
            "Querying revenue for %s (tenant %s) in %s: %s -> %s",
            property_id, tenant_id, tz_name, start_utc, end_utc,
        )

        query = text("""
            SELECT SUM(total_amount) AS total
            FROM reservations
            WHERE property_id = :property_id
              AND tenant_id = :tenant_id
              AND check_in_date >= :start_utc
              AND check_in_date < :end_utc
        """)
        result = await session.execute(query, {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "start_utc": start_utc,
            "end_utc": end_utc,
        })
        row = result.fetchone()
        # SUM() over no rows is NULL; Decimal(str(...)) keeps the exact scale.
        return Decimal(str(row.total)) if row and row.total is not None else Decimal("0")

    if db_session is not None:
        return await _run(db_session)

    db_pool = DatabasePool()
    await db_pool.initialize()
    async with db_pool.get_session() as session:
        return await _run(session)

async def calculate_total_revenue(property_id: str, tenant_id: str) -> Dict[str, Any]:
    """
    Aggregates revenue from database.
    """
    try:
        # Use the shared pool instance. Constructing a new DatabasePool() per call
        # built a fresh engine - and a fresh set of connections - on every request.
        from app.core.database_pool import db_pool

        # Initialize pool if needed (no-op once initialized)
        await db_pool.initialize()

        if db_pool.session_factory:
            async with db_pool.get_session() as session:
                # Use SQLAlchemy text for raw SQL
                from sqlalchemy import text
                
                query = text("""
                    SELECT 
                        property_id,
                        SUM(total_amount) as total_revenue,
                        COUNT(*) as reservation_count
                    FROM reservations 
                    WHERE property_id = :property_id AND tenant_id = :tenant_id
                    GROUP BY property_id
                """)
                
                result = await session.execute(query, {
                    "property_id": property_id, 
                    "tenant_id": tenant_id
                })
                row = result.fetchone()
                
                if row:
                    total_revenue = Decimal(str(row.total_revenue))
                    return {
                        "property_id": property_id,
                        "tenant_id": tenant_id,
                        "total": str(total_revenue),
                        "currency": "USD", 
                        "count": row.reservation_count
                    }
                else:
                    # No reservations found for this property
                    return {
                        "property_id": property_id,
                        "tenant_id": tenant_id,
                        "total": "0.00",
                        "currency": "USD",
                        "count": 0
                    }
        else:
            raise Exception("Database pool not available")
            
    except Exception as e:
        # Previously this swallowed every failure and returned hard-coded revenue
        # figures keyed by property_id. A finance dashboard silently served
        # fabricated totals whenever the database hiccuped, which is precisely the
        # "numbers don't match our internal records" the client reported - and it
        # is indistinguishable from real data at the UI. Surface the failure.
        logger.exception(
            "Revenue query failed for %s (tenant: %s)", property_id, tenant_id
        )
        raise

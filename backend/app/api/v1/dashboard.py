from fastapi import APIRouter, Depends, HTTPException, status
from typing import Dict, Any
from app.services.cache import get_revenue_summary
from app.core.auth import authenticate_request as get_current_user
from app.models.auth import AuthenticatedUser

router = APIRouter()

@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user)
) -> Dict[str, Any]:

    # A missing tenant must never fall back to a shared bucket: every user whose
    # tenant failed to resolve would land in the same 'default_tenant' scope and
    # read each other's revenue. Refuse the request instead.
    tenant_id = current_user.tenant_id
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tenant associated with this account",
        )

    revenue_data = await get_revenue_summary(property_id, tenant_id)

    return {
        "property_id": revenue_data['property_id'],
        # Money stays an exact decimal string end-to-end.
        #
        # total_amount is NUMERIC(10,3) (database/schema.sql) and the service
        # sums it as Decimal. float() then re-encodes that base-10 value as an
        # IEEE-754 binary double, which cannot represent most cent amounts
        # exactly - float('1080.40') is really 1080.400000000000090949...
        # The error is far below a cent per value, but it is real, it accumulates
        # over larger sums, and it makes totals fail to compare equal to the
        # client's own books. The third decimal is then lost outright when the UI
        # rounds to two places, so a 333.333 component silently becomes 333.33.
        # Serialising the Decimal as a string keeps the stored value intact and
        # lets the client decide how to round for display.
        "total_revenue": revenue_data['total'],
        "currency": revenue_data['currency'],
        "reservations_count": revenue_data['count']
    }

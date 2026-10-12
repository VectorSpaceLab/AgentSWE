from __future__ import annotations

from .models import Quote, QuoteRequest
from .policy import PricingPolicy


def calculate_quote(request: QuoteRequest, policy: PricingPolicy) -> Quote:
    kilos = request.parcel.billable_kilos
    transport = kilos * policy.zone_rate(request.destination)
    multiplier = policy.service_multiplier(request.service_level)
    transport = (transport * multiplier + 99) // 100
    insurance = (
        request.parcel.declared_value_cents * policy.insurance_basis_points + 9999
    ) // 10000
    amount = transport + insurance
    return Quote(
        account_id=request.account_id,
        destination=request.destination,
        service_level=request.service_level,
        currency=request.currency,
        amount_cents=amount,
        policy_revision=policy.revision,
    )

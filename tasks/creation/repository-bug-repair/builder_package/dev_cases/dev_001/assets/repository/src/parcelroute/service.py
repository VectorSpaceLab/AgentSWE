from __future__ import annotations

from .cache import QuoteCache
from .models import Parcel, QuoteRequest
from .policy import DEFAULT_POLICY, PricingPolicy
from .pricing import calculate_quote


class QuoteService:
    def __init__(
        self,
        policy: PricingPolicy = DEFAULT_POLICY,
        cache: QuoteCache | None = None,
    ) -> None:
        self.policy = policy
        self.cache = cache if cache is not None else QuoteCache()

    def quote_request(self, request: QuoteRequest) -> dict[str, object]:
        key = request.cache_identity(self.policy.revision)
        cached = self.cache.get(key)
        if cached is not None:
            return cached.as_dict()
        normalized = request.normalized()
        quote = calculate_quote(normalized, self.policy)
        self.cache.put(key, quote)
        return quote.as_dict()

    def quote(
        self,
        account_id: str,
        weight_grams: int,
        destination: str,
        service_level: str = "economy",
        declared_value_cents: int = 0,
        currency: str = "USD",
    ) -> dict[str, object]:
        request = QuoteRequest(
            account_id=account_id,
            destination=destination,
            service_level=service_level,
            parcel=Parcel(weight_grams, declared_value_cents),
            currency=currency,
        )
        return self.quote_request(request)

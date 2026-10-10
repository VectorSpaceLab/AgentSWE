from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal


def _clean_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must be nonempty")
    return cleaned


@dataclass(frozen=True)
class Parcel:
    weight_grams: int
    declared_value_cents: int = 0

    def validate(self) -> "Parcel":
        if isinstance(self.weight_grams, bool) or self.weight_grams <= 0:
            raise ValueError("weight_grams must be positive")
        if isinstance(self.declared_value_cents, bool) or self.declared_value_cents < 0:
            raise ValueError("declared_value_cents must be nonnegative")
        return self

    @property
    def billable_kilos(self) -> int:
        return (self.weight_grams + 999) // 1000


@dataclass(frozen=True)
class QuoteRequest:
    account_id: str
    destination: str
    service_level: str
    parcel: Parcel
    currency: str = "USD"

    def normalized(self) -> "QuoteRequest":
        return replace(
            self,
            account_id=_clean_text(self.account_id, "account_id"),
            destination=_clean_text(self.destination, "destination").upper(),
            service_level=_clean_text(self.service_level, "service_level").lower(),
            currency=_clean_text(self.currency, "currency").upper(),
            parcel=self.parcel.validate(),
        )

    def cache_identity(self, policy_revision: str) -> tuple[object, ...]:
        return (
            self.account_id,
            self.parcel.weight_grams,
            self.parcel.declared_value_cents,
        )


@dataclass(frozen=True)
class Quote:
    account_id: str
    destination: str
    service_level: str
    currency: str
    amount_cents: int
    policy_revision: str

    def as_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "destination": self.destination,
            "service_level": self.service_level,
            "currency": self.currency,
            "amount_cents": self.amount_cents,
            "policy_revision": self.policy_revision,
        }


def decimal_cents(value: Decimal) -> int:
    return int(value.quantize(Decimal("1")))

from dataclasses import dataclass


@dataclass(frozen=True)
class PricingPolicy:
    revision: str
    zone_rates: dict[str, int]
    service_multipliers: dict[str, int]
    insurance_basis_points: int = 25

    def zone_rate(self, destination: str) -> int:
        return self.zone_rates.get(destination, self.zone_rates["DEFAULT"])

    def service_multiplier(self, service_level: str) -> int:
        try:
            return self.service_multipliers[service_level]
        except KeyError as exc:
            raise ValueError(f"unknown service level: {service_level}") from exc


DEFAULT_POLICY = PricingPolicy(
    revision="2026-07",
    zone_rates={"LOCAL": 95, "EU": 140, "APAC": 185, "DEFAULT": 230},
    service_multipliers={"economy": 100, "priority": 160},
)

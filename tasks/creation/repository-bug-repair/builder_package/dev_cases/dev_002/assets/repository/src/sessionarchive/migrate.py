from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def _seconds_to_ms(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("expires must be numeric seconds")
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("expires must be numeric seconds") from exc
    if not seconds.is_finite() or seconds < 0:
        raise ValueError("expires must be finite and nonnegative")
    return int((seconds * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def migrate_record(raw: dict[str, object]) -> dict[str, object]:
    record = dict(raw)
    version = record.get("version", 1)
    if version == 2:
        return record
    if version != 1:
        raise ValueError(f"unsupported session version: {version!r}")
    if "expires" not in record:
        raise ValueError("legacy record is missing expires")
    record["expires_at_ms"] = _seconds_to_ms(record.pop("expires"))
    record["version"] = 2
    return record

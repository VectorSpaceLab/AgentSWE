from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def timeout_to_ms(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("timeout must be numeric")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("timeout must be numeric") from exc
    if not decimal.is_finite() or decimal < 0:
        raise ValueError("timeout must be finite and nonnegative")
    return int((decimal * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def decode_extension(payload: object) -> dict[str, object]:
    if not isinstance(payload, str):
        raise ValueError("extension payload must be JSON text")
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid extension JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("extension JSON must be an object")
    return value


def encode_extension(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))

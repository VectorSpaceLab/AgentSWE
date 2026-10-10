#!/usr/bin/env python3
"""Regenerate the eight synthetic repository assets for benchmark v4.

All material is synthetic and uses only the Python standard library.  The
generator intentionally emits the buggy starting snapshots, never reference
repairs or evaluator-owned hidden checks.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from textwrap import dedent


ROOT = Path(__file__).resolve().parents[1]


def project(name: str, version: str) -> str:
    return f'''[project]
name = "{name}"
version = "{version}"
requires-python = ">=3.10"
dependencies = []

[tool.setuptools]
package-dir = {{"" = "src"}}
'''


def write_tree(case_group: str, case_id: str, files: dict[str, str]) -> None:
    repository = ROOT / case_group / case_id / "assets" / "repository"
    if repository.exists():
        shutil.rmtree(repository)
    for relative, content in files.items():
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(dedent(content).lstrip("\n"), encoding="utf-8")


def dev_001() -> dict[str, str]:
    return {
        "README.md": """
            # ParcelRoute

            Deterministic parcel quotation library with request normalization,
            policy revisions, and an in-process quote cache.
        """,
        "pyproject.toml": project("parcelroute", "2.4.0"),
        "src/parcelroute/__init__.py": """
            from .models import Parcel, QuoteRequest
            from .service import QuoteService

            __all__ = ["Parcel", "QuoteRequest", "QuoteService"]
        """,
        "src/parcelroute/models.py": """
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
        """,
        "src/parcelroute/policy.py": """
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
        """,
        "src/parcelroute/pricing.py": """
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
        """,
        "src/parcelroute/cache.py": """
            from __future__ import annotations

            from collections import OrderedDict
            from threading import RLock

            from .models import Quote


            class QuoteCache:
                def __init__(self, capacity: int = 128) -> None:
                    if capacity <= 0:
                        raise ValueError("capacity must be positive")
                    self.capacity = capacity
                    self._lock = RLock()
                    self._entries: OrderedDict[tuple[object, ...], Quote] = OrderedDict()

                def get(self, key: tuple[object, ...]) -> Quote | None:
                    with self._lock:
                        quote = self._entries.get(key)
                        if quote is not None:
                            self._entries.move_to_end(key)
                        return quote

                def put(self, key: tuple[object, ...], quote: Quote) -> None:
                    with self._lock:
                        self._entries[key] = quote
                        self._entries.move_to_end(key)
                        while len(self._entries) > self.capacity:
                            self._entries.popitem(last=False)

                def clear(self) -> None:
                    with self._lock:
                        self._entries.clear()

                def __len__(self) -> int:
                    with self._lock:
                        return len(self._entries)
        """,
        "src/parcelroute/service.py": """
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
        """,
        "tests/test_service.py": """
            import sys
            import unittest
            from pathlib import Path

            sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

            from parcelroute import QuoteService


            class QuoteServiceTests(unittest.TestCase):
                def test_quote_shape_and_price(self):
                    result = QuoteService().quote("acct", 1500, "EU")
                    self.assertEqual(result["amount_cents"], 280)
                    self.assertEqual(result["destination"], "EU")

                def test_identical_request_reuses_cache(self):
                    service = QuoteService()
                    first = service.quote("acct", 500, "LOCAL")
                    second = service.quote("acct", 500, "LOCAL")
                    self.assertEqual(first, second)
                    self.assertEqual(len(service.cache), 1)

                def test_validation(self):
                    with self.assertRaisesRegex(ValueError, "destination"):
                        QuoteService().quote("acct", 1000, "")


            if __name__ == "__main__":
                unittest.main()
        """,
    }


def dev_002() -> dict[str, str]:
    return {
        "README.md": """
            # SessionArchive

            File-backed JSON-lines session archive with versioned records,
            atomic rewrites, and a rolling-upgrade migration entry point.
        """,
        "pyproject.toml": project("sessionarchive", "2.2.0"),
        "src/sessionarchive/__init__.py": """
            from .store import SessionArchive

            __all__ = ["SessionArchive"]
        """,
        "src/sessionarchive/model.py": """
            from __future__ import annotations

            from dataclasses import dataclass


            @dataclass(frozen=True)
            class Session:
                session_id: str
                user_id: str
                expires_at_ms: int
                metadata: dict[str, object]
                extension: dict[str, object]

                def as_dict(self) -> dict[str, object]:
                    value = {
                        "session_id": self.session_id,
                        "user_id": self.user_id,
                        "expires_at_ms": self.expires_at_ms,
                        "metadata": dict(self.metadata),
                    }
                    value.update(self.extension)
                    return value


            def ensure_mapping(value: object, field: str) -> dict[str, object]:
                if value is None:
                    return {}
                if not isinstance(value, dict):
                    raise ValueError(f"{field} must be an object")
                return dict(value)
        """,
        "src/sessionarchive/codec.py": """
            from __future__ import annotations

            import json

            from .model import Session, ensure_mapping


            CORE_FIELDS = {
                "version",
                "session_id",
                "user_id",
                "expires",
                "expires_at_ms",
                "metadata",
            }


            def encode_record(record: dict[str, object]) -> bytes:
                return (
                    json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    + "\\n"
                ).encode("utf-8")


            def decode_line(payload: bytes, line_number: int) -> dict[str, object]:
                try:
                    value = json.loads(payload.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid session record at line {line_number}") from exc
                if not isinstance(value, dict):
                    raise ValueError(f"session record at line {line_number} is not an object")
                return value


            def decode_v2(record: dict[str, object]) -> Session:
                if record.get("version") != 2:
                    raise ValueError(f"unsupported session version: {record.get('version')!r}")
                expires = record.get("expires_at_ms")
                if isinstance(expires, bool) or not isinstance(expires, int) or expires < 0:
                    raise ValueError("expires_at_ms must be a nonnegative integer")
                extension = {key: value for key, value in record.items() if key not in CORE_FIELDS}
                return Session(
                    session_id=str(record["session_id"]),
                    user_id=str(record["user_id"]),
                    expires_at_ms=expires,
                    metadata=ensure_mapping(record.get("metadata"), "metadata"),
                    extension=extension,
                )
        """,
        "src/sessionarchive/migrate.py": """
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
        """,
        "src/sessionarchive/io.py": """
            from __future__ import annotations

            import os
            from pathlib import Path


            def fsync_directory(path: Path) -> None:
                descriptor = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)


            def atomic_replace(path: Path, payload: bytes) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + ".tmp")
                with temporary.open("wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                fsync_directory(path.parent)


            def read_complete_lines(path: Path) -> list[bytes]:
                if not path.exists():
                    return []
                data = path.read_bytes()
                if not data:
                    return []
                if not data.endswith(b"\\n"):
                    raise ValueError("session archive has an incomplete final record")
                return data.splitlines()
        """,
        "src/sessionarchive/store.py": """
            from __future__ import annotations

            from pathlib import Path

            from .codec import decode_line, decode_v2, encode_record
            from .io import atomic_replace, read_complete_lines
            from .migrate import migrate_record


            class SessionArchive:
                def __init__(self, path: str | Path) -> None:
                    self.path = Path(path)

                def read_raw(self) -> list[dict[str, object]]:
                    return [
                        decode_line(line, line_number)
                        for line_number, line in enumerate(read_complete_lines(self.path), 1)
                    ]

                def load_all(self) -> list[dict[str, object]]:
                    sessions = []
                    for raw in self.read_raw():
                        sessions.append(decode_v2(migrate_record(raw)).as_dict())
                    return sessions

                def append_v2(self, raw: dict[str, object]) -> None:
                    record = migrate_record(raw)
                    decode_v2(record)
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("ab") as handle:
                        handle.write(encode_record(record))
                        handle.flush()

                def migrate_file(self) -> int:
                    records = self.read_raw()
                    migrated = [migrate_record(record) for record in records]
                    payload = b"".join(encode_record(record) for record in migrated)
                    self.path.write_bytes(payload)
                    return len(migrated)

                def restore(self, records: list[dict[str, object]]) -> None:
                    payload = b"".join(encode_record(record) for record in records)
                    atomic_replace(self.path, payload)
        """,
        "src/sessionarchive/migration_cli.py": """
            from __future__ import annotations

            import argparse
            import json
            from pathlib import Path

            from .store import SessionArchive


            def main(argv: list[str] | None = None) -> int:
                parser = argparse.ArgumentParser()
                parser.add_argument("--archive", required=True, type=Path)
                parser.add_argument("--mode", choices=["migrate", "inspect"], default="migrate")
                args = parser.parse_args(argv)
                archive = SessionArchive(args.archive)
                if args.mode == "inspect":
                    print(json.dumps(archive.load_all(), sort_keys=True))
                else:
                    print(json.dumps({"migrated": archive.migrate_file()}, sort_keys=True))
                return 0


            if __name__ == "__main__":
                raise SystemExit(main())
        """,
        "tests/test_archive.py": """
            import tempfile
            import sys
            import unittest
            from pathlib import Path

            sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

            from sessionarchive.codec import encode_record
            from sessionarchive.store import SessionArchive


            class ArchiveTests(unittest.TestCase):
                def test_unmarked_legacy_record_loads(self):
                    with tempfile.TemporaryDirectory() as tmp:
                        path = Path(tmp) / "sessions.jsonl"
                        path.write_bytes(encode_record({"session_id": "s", "user_id": "u", "expires": 2}))
                        self.assertEqual(SessionArchive(path).load_all()[0]["expires_at_ms"], 2000)

                def test_v2_round_trip(self):
                    with tempfile.TemporaryDirectory() as tmp:
                        archive = SessionArchive(Path(tmp) / "sessions.jsonl")
                        archive.append_v2({"version": 2, "session_id": "s", "user_id": "u", "expires_at_ms": 5})
                        self.assertEqual(archive.load_all()[0]["expires_at_ms"], 5)

                def test_unsupported_version(self):
                    with tempfile.TemporaryDirectory() as tmp:
                        path = Path(tmp) / "sessions.jsonl"
                        path.write_bytes(encode_record({"version": 8}))
                        with self.assertRaises(ValueError):
                            SessionArchive(path).load_all()


            if __name__ == "__main__":
                unittest.main()
        """,
    }


def test_001() -> dict[str, str]:
    return {
        "README.md": """
            # DispatchQueue

            Thread-safe delivery registration service backed by an append-only
            in-memory audit journal and immutable snapshots.
        """,
        "pyproject.toml": project("dispatchqueue", "1.7.0"),
        "src/dispatchqueue/__init__.py": """
            from .service import DispatchQueue

            __all__ = ["DispatchQueue"]
        """,
        "src/dispatchqueue/models.py": """
            from __future__ import annotations

            from dataclasses import dataclass
            from time import time_ns


            def require_text(value: str, field: str) -> str:
                if not isinstance(value, str):
                    raise TypeError(f"{field} must be text")
                value = value.strip()
                if not value:
                    raise ValueError(f"{field} must be nonempty")
                return value


            @dataclass(frozen=True)
            class Delivery:
                delivery_id: str
                worker: str
                payload: dict[str, object]
                sequence: int
                recorded_ns: int

                @classmethod
                def create(
                    cls,
                    delivery_id: str,
                    worker: str,
                    payload: dict[str, object],
                    sequence: int,
                ) -> "Delivery":
                    return cls(
                        require_text(delivery_id, "delivery_id"),
                        require_text(worker, "worker"),
                        dict(payload),
                        sequence,
                        time_ns(),
                    )

                def as_dict(self) -> dict[str, object]:
                    return {
                        "delivery_id": self.delivery_id,
                        "worker": self.worker,
                        "payload": dict(self.payload),
                        "sequence": self.sequence,
                        "recorded_ns": self.recorded_ns,
                    }
        """,
        "src/dispatchqueue/journal.py": """
            from __future__ import annotations

            import json
            import threading

            from .models import Delivery


            class AuditJournal:
                def __init__(self) -> None:
                    self._lock = threading.Lock()
                    self._lines: list[bytes] = []

                def append(self, delivery: Delivery) -> None:
                    payload = json.dumps(
                        delivery.as_dict(), sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                    with self._lock:
                        self._lines.append(payload)

                def lines(self) -> list[bytes]:
                    with self._lock:
                        return list(self._lines)

                def replay(self) -> list[dict[str, object]]:
                    records: list[dict[str, object]] = []
                    for line_number, payload in enumerate(self.lines(), 1):
                        try:
                            value = json.loads(payload.decode("utf-8"))
                        except (UnicodeError, json.JSONDecodeError) as exc:
                            raise ValueError(f"invalid audit row {line_number}") from exc
                        if not isinstance(value, dict):
                            raise ValueError(f"audit row {line_number} is not an object")
                        records.append(value)
                    return records

                def __len__(self) -> int:
                    with self._lock:
                        return len(self._lines)
        """,
        "src/dispatchqueue/state.py": """
            from __future__ import annotations

            import threading

            from .models import Delivery


            class QueueState:
                def __init__(self) -> None:
                    self._lock = threading.RLock()
                    self._by_id: dict[str, Delivery] = {}
                    self._ordered: list[Delivery] = []
                    self._next_sequence = 1

                def contains(self, delivery_id: str) -> bool:
                    with self._lock:
                        return delivery_id in self._by_id

                def next_sequence(self) -> int:
                    with self._lock:
                        value = self._next_sequence
                        self._next_sequence += 1
                        return value

                def commit(self, delivery: Delivery) -> None:
                    with self._lock:
                        self._by_id[delivery.delivery_id] = delivery
                        self._ordered.append(delivery)

                def rows(self) -> list[Delivery]:
                    with self._lock:
                        return list(self._ordered)

                def get(self, delivery_id: str) -> Delivery | None:
                    with self._lock:
                        return self._by_id.get(delivery_id)

                def size(self) -> int:
                    with self._lock:
                        return len(self._ordered)

                def reset_next_sequence(self, value: int) -> None:
                    if value <= 0:
                        raise ValueError("next sequence must be positive")
                    with self._lock:
                        self._next_sequence = value
        """,
        "src/dispatchqueue/service.py": """
            from __future__ import annotations

            import time

            from .journal import AuditJournal
            from .models import Delivery, require_text
            from .state import QueueState


            class DispatchQueue:
                def __init__(
                    self,
                    state: QueueState | None = None,
                    journal: AuditJournal | None = None,
                ) -> None:
                    self.state = state if state is not None else QueueState()
                    self.journal = journal if journal is not None else AuditJournal()

                def record_once(
                    self,
                    delivery_id: str,
                    worker: str,
                    payload: dict[str, object],
                ) -> bool:
                    delivery_id = require_text(delivery_id, "delivery_id")
                    if self.state.contains(delivery_id):
                        return False
                    time.sleep(0.001)
                    delivery = Delivery.create(
                        delivery_id,
                        worker,
                        payload,
                        self.state.next_sequence(),
                    )
                    self.journal.append(delivery)
                    self.state.commit(delivery)
                    return True

                def get(self, delivery_id: str) -> dict[str, object] | None:
                    delivery = self.state.get(delivery_id)
                    return None if delivery is None else delivery.as_dict()

                def snapshot(self) -> list[dict[str, object]]:
                    return [delivery.as_dict() for delivery in self.state.rows()]

                def audit_snapshot(self) -> list[dict[str, object]]:
                    return [dict(record) for record in self.journal.replay()]

                @classmethod
                def from_journal(cls, journal: AuditJournal) -> "DispatchQueue":
                    from .replay import rebuild_state

                    return cls(state=rebuild_state(journal), journal=journal)
        """,
        "src/dispatchqueue/consistency.py": """
            from __future__ import annotations

            from .service import DispatchQueue


            def audit_consistency(queue: DispatchQueue) -> dict[str, object]:
                rows = queue.snapshot()
                audit = queue.audit_snapshot()
                row_ids = [str(row["delivery_id"]) for row in rows]
                audit_ids = [str(row["delivery_id"]) for row in audit]
                return {
                    "row_count": len(rows),
                    "audit_count": len(audit),
                    "duplicate_rows": len(row_ids) - len(set(row_ids)),
                    "duplicate_audit": len(audit_ids) - len(set(audit_ids)),
                    "same_order": row_ids == audit_ids,
                }
        """,
        "src/dispatchqueue/replay.py": """
            from __future__ import annotations

            from .journal import AuditJournal
            from .models import Delivery
            from .state import QueueState


            def delivery_from_record(record: dict[str, object]) -> Delivery:
                payload = record.get("payload", {})
                if not isinstance(payload, dict):
                    raise ValueError("audit payload must be an object")
                sequence = record.get("sequence")
                recorded_ns = record.get("recorded_ns")
                if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
                    raise ValueError("audit sequence must be positive")
                if isinstance(recorded_ns, bool) or not isinstance(recorded_ns, int):
                    raise ValueError("audit recorded_ns must be an integer")
                return Delivery(
                    delivery_id=str(record["delivery_id"]),
                    worker=str(record["worker"]),
                    payload=dict(payload),
                    sequence=sequence,
                    recorded_ns=recorded_ns,
                )


            def rebuild_state(journal: AuditJournal) -> QueueState:
                state = QueueState()
                expected_sequence = 1
                seen: set[str] = set()
                for record in journal.replay():
                    delivery = delivery_from_record(record)
                    if delivery.sequence != expected_sequence:
                        raise ValueError("audit sequence is not contiguous")
                    if delivery.delivery_id in seen:
                        raise ValueError("audit contains a duplicate delivery")
                    state.commit(delivery)
                    seen.add(delivery.delivery_id)
                    expected_sequence += 1
                state.reset_next_sequence(expected_sequence)
                return state
        """,
        "src/dispatchqueue/metrics.py": """
            from __future__ import annotations

            from collections import Counter

            from .service import DispatchQueue


            def queue_metrics(queue: DispatchQueue) -> dict[str, object]:
                rows = queue.snapshot()
                workers = Counter(str(row["worker"]) for row in rows)
                payload_keys = Counter(
                    key
                    for row in rows
                    for key in dict(row.get("payload", {})).keys()
                )
                return {
                    "deliveries": len(rows),
                    "workers": dict(sorted(workers.items())),
                    "payload_keys": dict(sorted(payload_keys.items())),
                    "audit_rows": len(queue.audit_snapshot()),
                }
        """,
        "tests/test_queue.py": """
            import sys
            import unittest
            from pathlib import Path

            sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

            from dispatchqueue import DispatchQueue
            from dispatchqueue.consistency import audit_consistency


            class QueueTests(unittest.TestCase):
                def test_sequential_retry(self):
                    queue = DispatchQueue()
                    self.assertTrue(queue.record_once("d-1", "w", {"x": 1}))
                    self.assertFalse(queue.record_once("d-1", "w", {"x": 1}))
                    self.assertEqual(len(queue.snapshot()), 1)

                def test_order_and_defensive_snapshot(self):
                    queue = DispatchQueue()
                    queue.record_once("a", "w", {})
                    queue.record_once("b", "w", {})
                    rows = queue.snapshot()
                    self.assertEqual([row["delivery_id"] for row in rows], ["a", "b"])
                    rows[0]["payload"]["changed"] = True
                    self.assertEqual(queue.snapshot()[0]["payload"], {})

                def test_audit_matches_state(self):
                    queue = DispatchQueue()
                    queue.record_once("a", "w", {})
                    self.assertTrue(audit_consistency(queue)["same_order"])


            if __name__ == "__main__":
                unittest.main()
        """,
    }


def test_002() -> dict[str, str]:
    return {
        "README.md": """
            # TenantConfig

            SQLite-backed tenant settings with online v1-to-v3 migration and a
            compatibility repository used during rolling deployments.
        """,
        "pyproject.toml": project("tenantconfig", "3.1.0"),
        "src/tenantconfig/__init__.py": """
            from .repository import ConfigRepository
            from .schema import create_v1_database, create_v3_database

            __all__ = ["ConfigRepository", "create_v1_database", "create_v3_database"]
        """,
        "src/tenantconfig/db.py": """
            from __future__ import annotations

            import sqlite3
            from contextlib import contextmanager
            from pathlib import Path


            def connect(path: str | Path) -> sqlite3.Connection:
                connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")
                return connection


            @contextmanager
            def transaction(connection: sqlite3.Connection, mode: str = "IMMEDIATE"):
                connection.execute(f"BEGIN {mode}")
                try:
                    yield connection
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()


            def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
                return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}


            def user_version(connection: sqlite3.Connection) -> int:
                return int(connection.execute("PRAGMA user_version").fetchone()[0])
        """,
        "src/tenantconfig/schema.py": """
            from __future__ import annotations

            import json
            from pathlib import Path

            from .db import connect, transaction


            def create_v1_database(path: str | Path, rows: list[dict[str, object]]) -> None:
                connection = connect(path)
                try:
                    with transaction(connection):
                        connection.execute(
                            "CREATE TABLE tenant_settings ("
                            "tenant_id TEXT PRIMARY KEY, timeout_seconds TEXT NOT NULL, "
                            "retries INTEGER NOT NULL DEFAULT 2, payload_json TEXT NOT NULL)"
                        )
                        for row in rows:
                            payload = dict(row.get("payload", {}))
                            connection.execute(
                                "INSERT INTO tenant_settings "
                                "(tenant_id, timeout_seconds, retries, payload_json) VALUES (?, ?, ?, ?)",
                                (
                                    str(row["tenant_id"]),
                                    str(row["timeout"]),
                                    int(row.get("retries", 2)),
                                    json.dumps(payload, sort_keys=True),
                                ),
                            )
                        connection.execute("PRAGMA user_version=1")
                finally:
                    connection.close()


            def create_v3_database(path: str | Path) -> None:
                connection = connect(path)
                try:
                    with transaction(connection):
                        connection.execute(
                            "CREATE TABLE tenant_settings ("
                            "tenant_id TEXT PRIMARY KEY, timeout_ms INTEGER NOT NULL, "
                            "retries INTEGER NOT NULL DEFAULT 2, label TEXT NOT NULL DEFAULT 'default', "
                            "extension_json TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 1)"
                        )
                        connection.execute(
                            "CREATE TABLE migration_state (name TEXT PRIMARY KEY, phase TEXT NOT NULL)"
                        )
                        connection.execute("PRAGMA user_version=3")
                finally:
                    connection.close()
        """,
        "src/tenantconfig/codec.py": """
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
        """,
        "src/tenantconfig/repository.py": """
            from __future__ import annotations

            from pathlib import Path

            from .codec import decode_extension, timeout_to_ms
            from .db import connect, table_columns, user_version


            class ConfigRepository:
                def __init__(self, path: str | Path) -> None:
                    self.path = Path(path)

                def load(self, tenant_id: str) -> dict[str, object]:
                    connection = connect(self.path)
                    try:
                        version = user_version(connection)
                        if version == 1:
                            row = connection.execute(
                                "SELECT tenant_id, timeout_seconds, retries, payload_json "
                                "FROM tenant_settings WHERE tenant_id=?",
                                (tenant_id,),
                            ).fetchone()
                            if row is None:
                                raise KeyError(tenant_id)
                            payload = decode_extension(row["payload_json"])
                            return {
                                "tenant_id": str(row["tenant_id"]),
                                "timeout_ms": timeout_to_ms(row["timeout_seconds"]),
                                "retries": int(row["retries"]),
                                "label": str(payload.pop("label", "default")),
                                "extension": payload,
                                "revision": 0,
                            }
                        if version != 3:
                            raise ValueError(f"unsupported schema version: {version}")
                        row = connection.execute(
                            "SELECT tenant_id, timeout_ms, retries, label, extension_json, revision "
                            "FROM tenant_settings WHERE tenant_id=?",
                            (tenant_id,),
                        ).fetchone()
                        if row is None:
                            raise KeyError(tenant_id)
                        return {
                            "tenant_id": str(row["tenant_id"]),
                            "timeout_ms": int(row["timeout_ms"]),
                            "retries": int(row["retries"]),
                            "label": str(row["label"]),
                            "extension": decode_extension(row["extension_json"]),
                            "revision": int(row["revision"]),
                        }
                    finally:
                        connection.close()

                def schema_columns(self) -> set[str]:
                    connection = connect(self.path)
                    try:
                        return table_columns(connection, "tenant_settings")
                    finally:
                        connection.close()
        """,
        "src/tenantconfig/migrate.py": """
            from __future__ import annotations

            import json
            import sqlite3
            from pathlib import Path

            from .db import connect, table_columns, user_version


            def migrate(path: str | Path) -> int:
                connection = connect(path)
                try:
                    version = user_version(connection)
                    if version == 3:
                        return 0
                    if version != 1:
                        raise ValueError(f"unsupported schema version: {version}")
                    rows = list(connection.execute("SELECT * FROM tenant_settings ORDER BY tenant_id"))
                    connection.execute("PRAGMA user_version=3")
                    connection.execute("ALTER TABLE tenant_settings RENAME TO tenant_settings_v1")
                    connection.execute(
                        "CREATE TABLE tenant_settings ("
                        "tenant_id TEXT PRIMARY KEY, timeout_ms INTEGER NOT NULL, "
                        "retries INTEGER NOT NULL, label TEXT NOT NULL, "
                        "extension_json TEXT NOT NULL, revision INTEGER NOT NULL)"
                    )
                    for row in rows:
                        payload = json.loads(row["payload_json"])
                        label = str(payload.pop("label", "default"))
                        timeout_ms = int(row["timeout_seconds"]) * 1000
                        connection.execute(
                            "INSERT INTO tenant_settings VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                row["tenant_id"], timeout_ms, row["retries"], label,
                                json.dumps(payload, sort_keys=True), 1,
                            ),
                        )
                    connection.execute(
                        "CREATE TABLE migration_state (name TEXT PRIMARY KEY, phase TEXT NOT NULL)"
                    )
                    return len(rows)
                finally:
                    connection.close()


            def rollback(path: str | Path) -> None:
                connection = connect(path)
                try:
                    columns = table_columns(connection, "tenant_settings")
                    if "timeout_seconds" in columns:
                        return
                    if not table_columns(connection, "tenant_settings_v1"):
                        raise ValueError("no v1 rollback table")
                    connection.execute("DROP TABLE tenant_settings")
                    connection.execute("ALTER TABLE tenant_settings_v1 RENAME TO tenant_settings")
                    connection.execute("PRAGMA user_version=1")
                finally:
                    connection.close()
        """,
        "src/tenantconfig/migration_cli.py": """
            from __future__ import annotations

            import argparse
            import json
            from pathlib import Path

            from .migrate import migrate, rollback
            from .repository import ConfigRepository


            def main(argv: list[str] | None = None) -> int:
                parser = argparse.ArgumentParser()
                parser.add_argument("--db", required=True, type=Path)
                parser.add_argument("mode", choices=["migrate", "rollback", "inspect"])
                args = parser.parse_args(argv)
                if args.mode == "migrate":
                    result = {"migrated": migrate(args.db)}
                elif args.mode == "rollback":
                    rollback(args.db)
                    result = {"rolled_back": True}
                else:
                    result = {"columns": sorted(ConfigRepository(args.db).schema_columns())}
                print(json.dumps(result, sort_keys=True))
                return 0


            if __name__ == "__main__":
                raise SystemExit(main())
        """,
        "src/tenantconfig/diagnostics.py": """
            from __future__ import annotations

            from pathlib import Path

            from .db import connect, table_columns, user_version


            def inspect_database(path: str | Path) -> dict[str, object]:
                connection = connect(path)
                try:
                    tables = [
                        str(row[0])
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                        )
                    ]
                    columns = {
                        table: sorted(table_columns(connection, table))
                        for table in tables
                    }
                    counts = {
                        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                        for table in tables
                    }
                    return {
                        "user_version": user_version(connection),
                        "tables": tables,
                        "columns": columns,
                        "counts": counts,
                        "integrity": str(connection.execute("PRAGMA integrity_check").fetchone()[0]),
                    }
                finally:
                    connection.close()


            def migration_phase(path: str | Path) -> str:
                info = inspect_database(path)
                tables = set(info["tables"])
                version = info["user_version"]
                if version == 1 and "tenant_settings" in tables:
                    return "v1"
                if version == 3 and "tenant_settings_v1" in tables and "tenant_settings" not in tables:
                    return "interrupted_after_rename"
                if version == 3 and "tenant_settings" in tables:
                    return "v3"
                return "unknown"
        """,
        "src/tenantconfig/backup.py": """
            from __future__ import annotations

            import os
            import sqlite3
            from pathlib import Path

            from .db import connect


            def backup_database(source: str | Path, destination: str | Path) -> None:
                source_path = Path(source)
                destination_path = Path(destination)
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination_path.with_name(destination_path.name + ".tmp")
                if temporary.exists():
                    temporary.unlink()
                source_connection = connect(source_path)
                target_connection = sqlite3.connect(temporary)
                try:
                    source_connection.backup(target_connection)
                    target_connection.commit()
                finally:
                    target_connection.close()
                    source_connection.close()
                os.replace(temporary, destination_path)


            def restore_database(backup: str | Path, destination: str | Path) -> None:
                backup_database(backup, destination)
        """,
        "tests/test_repository.py": """
            import tempfile
            import sys
            import unittest
            from pathlib import Path

            sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

            from tenantconfig import ConfigRepository, create_v1_database, create_v3_database


            class RepositoryTests(unittest.TestCase):
                def test_v1_integer_timeout_is_readable(self):
                    with tempfile.TemporaryDirectory() as tmp:
                        path = Path(tmp) / "config.db"
                        create_v1_database(path, [{"tenant_id": "t", "timeout": 2}])
                        self.assertEqual(ConfigRepository(path).load("t")["timeout_ms"], 2000)

                def test_empty_v3_schema(self):
                    with tempfile.TemporaryDirectory() as tmp:
                        path = Path(tmp) / "config.db"
                        create_v3_database(path)
                        self.assertIn("timeout_ms", ConfigRepository(path).schema_columns())

                def test_missing_tenant(self):
                    with tempfile.TemporaryDirectory() as tmp:
                        path = Path(tmp) / "config.db"
                        create_v1_database(path, [])
                        with self.assertRaises(KeyError):
                            ConfigRepository(path).load("missing")


            if __name__ == "__main__":
                unittest.main()
        """,
    }


def test_003() -> dict[str, str]:
    return {
        "README.md": """
            # EventMerge

            Stable merge and exact de-duplication for ordered event streams.
        """,
        "pyproject.toml": project("eventmerge", "0.9.0"),
        "src/eventmerge/__init__.py": """
            from .merge import merge_events

            __all__ = ["merge_events"]
        """,
        "src/eventmerge/models.py": """
            from __future__ import annotations

            from dataclasses import dataclass


            @dataclass(frozen=True)
            class Event:
                timestamp_ms: int
                event_id: str
                payload: dict[str, object]

                @classmethod
                def from_mapping(cls, value: dict[str, object]) -> "Event":
                    timestamp = value.get("timestamp_ms")
                    event_id = value.get("event_id")
                    payload = value.get("payload", {})
                    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
                        raise ValueError("timestamp_ms must be an integer")
                    if not isinstance(event_id, str) or not event_id:
                        raise ValueError("event_id must be nonempty text")
                    if not isinstance(payload, dict):
                        raise ValueError("payload must be an object")
                    return cls(timestamp, event_id, dict(payload))

                def as_dict(self) -> dict[str, object]:
                    return {
                        "timestamp_ms": self.timestamp_ms,
                        "event_id": self.event_id,
                        "payload": dict(self.payload),
                    }
        """,
        "src/eventmerge/identity.py": """
            from __future__ import annotations

            from .models import Event


            def identity(event: Event) -> str:
                return event.event_id.casefold()


            def first_occurrence(events: list[Event]) -> list[Event]:
                result: list[Event] = []
                keys: list[str] = []
                for event in events:
                    key = identity(event)
                    if key not in keys:
                        keys.append(key)
                        result.append(event)
                return result
        """,
        "src/eventmerge/cursor.py": """
            from __future__ import annotations

            from collections.abc import Iterable, Iterator

            from .models import Event


            class Cursor:
                def __init__(self, source_index: int, values: Iterable[dict[str, object]]) -> None:
                    self.source_index = source_index
                    self._iterator: Iterator[dict[str, object]] = iter(values)
                    self.offset = -1
                    self.current: Event | None = None
                    self.advance()

                def advance(self) -> None:
                    try:
                        raw = next(self._iterator)
                    except StopIteration:
                        self.current = None
                        return
                    self.offset += 1
                    event = Event.from_mapping(raw)
                    if self.current is not None and event.timestamp_ms < self.current.timestamp_ms:
                        raise ValueError(f"source {self.source_index} is not ordered")
                    self.current = event

                def ordering_key(self) -> tuple[int, int, int]:
                    if self.current is None:
                        raise RuntimeError("exhausted cursor")
                    return (self.current.timestamp_ms, self.source_index, self.offset)
        """,
        "src/eventmerge/planner.py": """
            from __future__ import annotations

            from .cursor import Cursor


            def pick_next(cursors: list[Cursor]) -> Cursor | None:
                active = [cursor for cursor in cursors if cursor.current is not None]
                if not active:
                    return None
                active.sort(key=lambda cursor: cursor.ordering_key())
                return active[0]
        """,
        "src/eventmerge/merge.py": """
            from __future__ import annotations

            from collections.abc import Iterable

            from .cursor import Cursor
            from .identity import first_occurrence
            from .planner import pick_next


            def merge_events(
                sources: Iterable[Iterable[dict[str, object]]],
            ) -> list[dict[str, object]]:
                cursors = [Cursor(index, source) for index, source in enumerate(sources)]
                ordered = []
                while True:
                    cursor = pick_next(cursors)
                    if cursor is None:
                        break
                    assert cursor.current is not None
                    ordered.append(cursor.current)
                    cursor.advance()
                return [event.as_dict() for event in first_occurrence(ordered)]
        """,
        "src/eventmerge/validation.py": """
            from __future__ import annotations


            def summarize(events: list[dict[str, object]]) -> dict[str, object]:
                timestamps = [int(event["timestamp_ms"]) for event in events]
                ids = [str(event["event_id"]) for event in events]
                return {
                    "count": len(events),
                    "ordered": timestamps == sorted(timestamps),
                    "unique_exact_ids": len(ids) == len(set(ids)),
                    "first_timestamp": timestamps[0] if timestamps else None,
                    "last_timestamp": timestamps[-1] if timestamps else None,
                }
        """,
        "src/eventmerge/sources.py": """
            from __future__ import annotations

            from collections.abc import Iterable

            from .cursor import Cursor


            def build_cursors(
                sources: Iterable[Iterable[dict[str, object]]],
                maximum_sources: int = 256,
            ) -> list[Cursor]:
                cursors: list[Cursor] = []
                for index, source in enumerate(sources):
                    if index >= maximum_sources:
                        raise ValueError("too many event sources")
                    cursors.append(Cursor(index, source))
                return cursors


            def active_count(cursors: list[Cursor]) -> int:
                return sum(cursor.current is not None for cursor in cursors)


            def cursor_positions(cursors: list[Cursor]) -> list[dict[str, object]]:
                return [
                    {
                        "source_index": cursor.source_index,
                        "offset": cursor.offset,
                        "active": cursor.current is not None,
                        "timestamp_ms": None if cursor.current is None else cursor.current.timestamp_ms,
                    }
                    for cursor in cursors
                ]
        """,
        "src/eventmerge/result.py": """
            from __future__ import annotations

            from dataclasses import dataclass

            from .models import Event


            @dataclass
            class MergeResult:
                events: list[Event]
                consumed: int = 0
                duplicates: int = 0

                def observe(self, event: Event, duplicate: bool) -> None:
                    self.consumed += 1
                    if duplicate:
                        self.duplicates += 1
                    else:
                        self.events.append(event)

                def dictionaries(self) -> list[dict[str, object]]:
                    return [event.as_dict() for event in self.events]

                def stats(self) -> dict[str, int]:
                    return {
                        "consumed": self.consumed,
                        "emitted": len(self.events),
                        "duplicates": self.duplicates,
                    }
        """,
        "src/eventmerge/heapplanner.py": """
            from __future__ import annotations

            import heapq

            from .cursor import Cursor


            class HeapPlanner:
                def __init__(self, cursors: list[Cursor]) -> None:
                    self._heap: list[tuple[tuple[int, int, int], Cursor]] = []
                    for cursor in cursors:
                        if cursor.current is not None:
                            heapq.heappush(self._heap, (cursor.ordering_key(), cursor))

                def pop(self) -> Cursor | None:
                    if not self._heap:
                        return None
                    _key, cursor = heapq.heappop(self._heap)
                    return cursor

                def push_after_advance(self, cursor: Cursor) -> None:
                    cursor.advance()
                    if cursor.current is not None:
                        heapq.heappush(self._heap, (cursor.ordering_key(), cursor))

                def __len__(self) -> int:
                    return len(self._heap)
        """,
        "src/eventmerge/policy.py": """
            from __future__ import annotations

            from dataclasses import dataclass


            @dataclass(frozen=True)
            class MergePolicy:
                maximum_sources: int = 256
                require_exact_ids: bool = True
                preserve_source_ties: bool = True

                def validate(self) -> None:
                    if self.maximum_sources <= 0:
                        raise ValueError("maximum_sources must be positive")
                    if not self.require_exact_ids:
                        raise ValueError("only exact event identity is supported")
                    if not self.preserve_source_ties:
                        raise ValueError("source-order ties must be preserved")
        """,
        "src/eventmerge/jsonio.py": """
            from __future__ import annotations

            import json
            from collections.abc import Iterable


            def read_json_lines(lines: Iterable[str]) -> list[dict[str, object]]:
                events = []
                for line_number, line in enumerate(lines, 1):
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"invalid JSON at line {line_number}") from exc
                    if not isinstance(value, dict):
                        raise ValueError(f"line {line_number} is not an object")
                    events.append(value)
                return events


            def write_json_lines(events: list[dict[str, object]]) -> str:
                return "".join(
                    json.dumps(event, sort_keys=True, separators=(",", ":")) + "\\n"
                    for event in events
                )
        """,
        "tests/test_merge.py": """
            import sys
            import unittest
            from pathlib import Path

            sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

            from eventmerge import merge_events


            def event(ts, event_id):
                return {"timestamp_ms": ts, "event_id": event_id, "payload": {"id": event_id}}


            class MergeTests(unittest.TestCase):
                def test_order_and_duplicate(self):
                    result = merge_events([[event(1, "a"), event(3, "c")], [event(2, "b"), event(4, "a")]])
                    self.assertEqual([item["event_id"] for item in result], ["a", "b", "c"])

                def test_stable_ties(self):
                    result = merge_events([[event(1, "left")], [event(1, "right")]])
                    self.assertEqual([item["event_id"] for item in result], ["left", "right"])

                def test_rejects_unordered_source(self):
                    with self.assertRaises(ValueError):
                        merge_events([[event(2, "a"), event(1, "b")]])


            if __name__ == "__main__":
                unittest.main()
        """,
    }


def test_004() -> dict[str, str]:
    return {
        "README.md": """
            # WireBatch

            Incremental parser for a small length-prefixed request protocol.
            The benchmark case provides the pinned protocol profile.
        """,
        "pyproject.toml": project("wirebatch", "1.3.0"),
        "src/wirebatch/__init__.py": """
            from .errors import ProtocolError
            from .parser import FrameParser

            __all__ = ["FrameParser", "ProtocolError"]
        """,
        "src/wirebatch/errors.py": """
            class ProtocolError(ValueError):
                def __init__(self, code: str, message: str) -> None:
                    super().__init__(message)
                    self.code = code
        """,
        "src/wirebatch/models.py": """
            from __future__ import annotations

            from dataclasses import dataclass


            @dataclass(frozen=True)
            class Frame:
                headers: dict[str, str]
                payload: bytes

                def as_dict(self) -> dict[str, object]:
                    return {"headers": dict(self.headers), "payload": self.payload}
        """,
        "src/wirebatch/buffer.py": """
            from __future__ import annotations

            from .errors import ProtocolError


            class ByteBuffer:
                def __init__(self, limit: int) -> None:
                    self.limit = limit
                    self._data = bytearray()

                def append(self, chunk: bytes) -> None:
                    if not isinstance(chunk, bytes):
                        raise TypeError("feed expects bytes")
                    self._data.extend(chunk)
                    if len(self._data) > self.limit:
                        raise ProtocolError("buffer_limit", "parser buffer limit exceeded")

                def find(self, marker: bytes) -> int:
                    return self._data.find(marker)

                def take(self, count: int) -> bytes:
                    if count < 0 or count > len(self._data):
                        raise RuntimeError("invalid buffer take")
                    value = bytes(self._data[:count])
                    del self._data[:count]
                    return value

                def peek(self, count: int | None = None) -> bytes:
                    if count is None:
                        return bytes(self._data)
                    return bytes(self._data[:count])

                def __len__(self) -> int:
                    return len(self._data)
        """,
        "src/wirebatch/headers.py": """
            from __future__ import annotations

            import hashlib

            from .errors import ProtocolError


            def parse_length(line: bytes, maximum: int) -> int:
                if not line.startswith(b"LEN "):
                    raise ProtocolError("length_line", "frame must start with LEN")
                token = line[4:]
                if not token or not token.isdigit():
                    raise ProtocolError("length_value", "length must be decimal digits")
                value = int(token)
                if value > maximum:
                    raise ProtocolError("payload_limit", "payload is too large")
                return value


            def parse_headers(lines: list[bytes]) -> dict[str, str]:
                result: dict[str, str] = {}
                for raw in lines:
                    if b":" not in raw:
                        raise ProtocolError("header_syntax", "header lacks colon")
                    name_raw, value_raw = raw.split(b":", 1)
                    try:
                        name = name_raw.decode("ascii").strip().lower()
                        value = value_raw.decode("utf-8").strip()
                    except UnicodeError as exc:
                        raise ProtocolError("header_encoding", "invalid header encoding") from exc
                    if not name or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in name):
                        raise ProtocolError("header_name", "invalid header name")
                    if name in result:
                        raise ProtocolError("duplicate_header", f"duplicate header: {name}")
                    result[name] = value
                if "content-type" not in result:
                    raise ProtocolError("missing_header", "content-type is required")
                return result


            def verify_checksum(headers: dict[str, str], payload: bytes) -> None:
                expected = headers.get("checksum-sha256")
                if expected is None:
                    return
                actual = hashlib.sha256(payload).hexdigest()
                if len(expected) != 64 or expected.lower() != actual:
                    raise ProtocolError("checksum", "payload checksum mismatch")
        """,
        "src/wirebatch/parser.py": """
            from __future__ import annotations

            from .buffer import ByteBuffer
            from .errors import ProtocolError
            from .headers import parse_headers, parse_length, verify_checksum
            from .models import Frame


            class FrameParser:
                def __init__(
                    self,
                    max_payload_bytes: int = 1024 * 1024,
                    max_header_bytes: int = 8192,
                ) -> None:
                    self.max_payload_bytes = max_payload_bytes
                    self.max_header_bytes = max_header_bytes
                    self.buffer = ByteBuffer(max_payload_bytes + max_header_bytes + 64)

                def feed(self, chunk: bytes) -> list[dict[str, object]]:
                    self.buffer.append(chunk)
                    frames: list[dict[str, object]] = []
                    while len(self.buffer):
                        boundary = self.buffer.find(b"\\r\\n\\r\\n")
                        if boundary < 0:
                            if len(self.buffer) > self.max_header_bytes:
                                raise ProtocolError("header_limit", "header block is too large")
                            break
                        header_block = self.buffer.take(boundary + 4)[:-4]
                        lines = header_block.split(b"\\r\\n")
                        length = parse_length(lines[0], self.max_payload_bytes)
                        headers = parse_headers(lines[1:])
                        if len(self.buffer) < length + 2:
                            break
                        payload = self.buffer.take(length)
                        if self.buffer.take(2) != b"\\r\\n":
                            raise ProtocolError("frame_terminator", "payload lacks trailing CRLF")
                        verify_checksum(headers, payload)
                        frames.append(Frame(headers, payload).as_dict())
                    return frames

                def finish(self) -> None:
                    if len(self.buffer):
                        raise ProtocolError("truncated_frame", "stream ended mid-frame")
        """,
        "src/wirebatch/encode.py": """
            from __future__ import annotations

            import hashlib


            def encode_frame(
                payload: bytes,
                content_type: str = "application/octet-stream",
                request_id: str | None = None,
                checksum: bool = False,
            ) -> bytes:
                headers = [f"LEN {len(payload)}", f"Content-Type: {content_type}"]
                if request_id is not None:
                    headers.append(f"X-Request-ID: {request_id}")
                if checksum:
                    headers.append(f"Checksum-SHA256: {hashlib.sha256(payload).hexdigest()}")
                return ("\\r\\n".join(headers) + "\\r\\n\\r\\n").encode("utf-8") + payload + b"\\r\\n"
        """,
        "src/wirebatch/state.py": """
            from __future__ import annotations

            from dataclasses import dataclass


            @dataclass
            class PendingFrame:
                length: int
                headers: dict[str, str]

                def remaining_bytes(self, buffered: int) -> int:
                    return max(0, self.length + 2 - buffered)

                def ready(self, buffered: int) -> bool:
                    return buffered >= self.length + 2

                def describe(self) -> dict[str, object]:
                    return {"length": self.length, "headers": dict(self.headers)}


            @dataclass
            class ParserStats:
                bytes_received: int = 0
                frames_emitted: int = 0
                largest_buffer: int = 0

                def on_feed(self, chunk_size: int, buffer_size: int) -> None:
                    self.bytes_received += chunk_size
                    self.largest_buffer = max(self.largest_buffer, buffer_size)

                def on_frame(self) -> None:
                    self.frames_emitted += 1

                def as_dict(self) -> dict[str, int]:
                    return {
                        "bytes_received": self.bytes_received,
                        "frames_emitted": self.frames_emitted,
                        "largest_buffer": self.largest_buffer,
                    }
        """,
        "src/wirebatch/validation.py": """
            from __future__ import annotations

            from .errors import ProtocolError


            def validate_limits(max_payload_bytes: int, max_header_bytes: int) -> None:
                for value, name in [
                    (max_payload_bytes, "max_payload_bytes"),
                    (max_header_bytes, "max_header_bytes"),
                ]:
                    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                        raise ValueError(f"{name} must be a positive integer")
                if max_header_bytes > 1024 * 1024:
                    raise ValueError("max_header_bytes is unreasonably large")


            def validate_terminator(value: bytes) -> None:
                if value != b"\\r\\n":
                    raise ProtocolError("frame_terminator", "payload lacks trailing CRLF")


            def validate_finished(buffered: int, pending: bool) -> None:
                if buffered or pending:
                    raise ProtocolError("truncated_frame", "stream ended mid-frame")
        """,
        "src/wirebatch/diagnostics.py": """
            from __future__ import annotations

            from .errors import ProtocolError


            def error_record(error: ProtocolError) -> dict[str, str]:
                return {"code": error.code, "message": str(error)}


            def frame_summary(frame: dict[str, object]) -> dict[str, object]:
                headers = frame.get("headers", {})
                payload = frame.get("payload", b"")
                return {
                    "header_names": sorted(dict(headers).keys()),
                    "payload_bytes": len(payload),
                    "content_type": dict(headers).get("content-type"),
                }
        """,
        "src/wirebatch/stream.py": """
            from __future__ import annotations

            from collections.abc import Iterable

            from .parser import FrameParser


            def parse_chunks(
                chunks: Iterable[bytes],
                max_payload_bytes: int = 1024 * 1024,
                max_header_bytes: int = 8192,
            ) -> list[dict[str, object]]:
                parser = FrameParser(max_payload_bytes, max_header_bytes)
                frames: list[dict[str, object]] = []
                for chunk in chunks:
                    frames.extend(parser.feed(chunk))
                parser.finish()
                return frames


            def fixed_chunks(payload: bytes, size: int) -> list[bytes]:
                if size <= 0:
                    raise ValueError("chunk size must be positive")
                return [payload[index:index + size] for index in range(0, len(payload), size)]
        """,
        "tests/test_parser.py": """
            import sys
            import unittest
            from pathlib import Path

            sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

            from wirebatch import FrameParser, ProtocolError
            from wirebatch.encode import encode_frame


            class ParserTests(unittest.TestCase):
                def test_complete_frame(self):
                    parser = FrameParser()
                    frames = parser.feed(encode_frame(b"hello", request_id="r1"))
                    self.assertEqual(frames[0]["payload"], b"hello")
                    self.assertEqual(frames[0]["headers"]["x-request-id"], "r1")
                    parser.finish()

                def test_two_complete_frames(self):
                    parser = FrameParser()
                    frames = parser.feed(encode_frame(b"a") + encode_frame(b"b"))
                    self.assertEqual([frame["payload"] for frame in frames], [b"a", b"b"])

                def test_bad_length(self):
                    with self.assertRaises(ProtocolError):
                        FrameParser().feed(b"LEN -1\\r\\nContent-Type: x\\r\\n\\r\\n")


            if __name__ == "__main__":
                unittest.main()
        """,
    }


def test_005() -> dict[str, str]:
    return {
        "README.md": """
            # ProfileDirectory

            Multi-tenant profile service with revisioned repository writes,
            positive/negative caching, and an invalidation event bus.
        """,
        "pyproject.toml": project("profiledirectory", "2.8.0"),
        "src/profiledirectory/__init__.py": """
            from .models import Profile
            from .service import DirectoryService

            __all__ = ["DirectoryService", "Profile"]
        """,
        "src/profiledirectory/models.py": """
            from __future__ import annotations

            from dataclasses import dataclass, field, replace


            def clean(value: str, field: str) -> str:
                if not isinstance(value, str):
                    raise TypeError(f"{field} must be text")
                value = value.strip()
                if not value:
                    raise ValueError(f"{field} must be nonempty")
                return value


            @dataclass(frozen=True)
            class Profile:
                tenant_id: str
                user_id: str
                email: str
                display_name: str
                revision: int = 1
                attributes: dict[str, object] = field(default_factory=dict)

                def updated(self, *, email: str | None = None, display_name: str | None = None) -> "Profile":
                    return replace(
                        self,
                        email=clean(email, "email") if email is not None else self.email,
                        display_name=clean(display_name, "display_name") if display_name is not None else self.display_name,
                        revision=self.revision + 1,
                    )

                def as_dict(self) -> dict[str, object]:
                    return {
                        "tenant_id": self.tenant_id,
                        "user_id": self.user_id,
                        "email": self.email,
                        "display_name": self.display_name,
                        "revision": self.revision,
                        "attributes": dict(self.attributes),
                    }
        """,
        "src/profiledirectory/repository.py": """
            from __future__ import annotations

            import threading

            from .models import Profile


            class ProfileRepository:
                def __init__(self, profiles: list[Profile] | None = None) -> None:
                    self._lock = threading.RLock()
                    self._profiles = {
                        (profile.tenant_id, profile.user_id): profile
                        for profile in (profiles or [])
                    }

                def get(self, tenant_id: str, user_id: str) -> Profile | None:
                    with self._lock:
                        return self._profiles.get((tenant_id, user_id))

                def put(self, profile: Profile, expected_revision: int | None = None) -> Profile:
                    key = (profile.tenant_id, profile.user_id)
                    with self._lock:
                        current = self._profiles.get(key)
                        if expected_revision is not None:
                            actual = 0 if current is None else current.revision
                            if actual != expected_revision:
                                raise ValueError("revision conflict")
                        self._profiles[key] = profile
                        return profile

                def update(
                    self,
                    tenant_id: str,
                    user_id: str,
                    *,
                    email: str | None = None,
                    display_name: str | None = None,
                ) -> Profile:
                    with self._lock:
                        key = (tenant_id, user_id)
                        current = self._profiles[key]
                        updated = current.updated(email=email, display_name=display_name)
                        self._profiles[key] = updated
                        return updated

                def snapshot(self) -> list[Profile]:
                    with self._lock:
                        return list(self._profiles.values())
        """,
        "src/profiledirectory/cache.py": """
            from __future__ import annotations

            import threading
            from dataclasses import dataclass

            from .models import Profile


            @dataclass(frozen=True)
            class CacheEntry:
                profile: Profile | None


            class ProfileCache:
                def __init__(self) -> None:
                    self._lock = threading.RLock()
                    self._entries: dict[tuple[str, str], CacheEntry] = {}

                def lookup(self, tenant_id: str, user_id: str) -> tuple[bool, Profile | None]:
                    with self._lock:
                        entry = self._entries.get((tenant_id, user_id))
                        return (False, None) if entry is None else (True, entry.profile)

                def put(self, tenant_id: str, user_id: str, profile: Profile | None) -> None:
                    with self._lock:
                        self._entries[(tenant_id, user_id)] = CacheEntry(profile)

                def invalidate(self, user_id: str) -> None:
                    with self._lock:
                        self._entries.pop(user_id, None)

                def size(self) -> int:
                    with self._lock:
                        return len(self._entries)
        """,
        "src/profiledirectory/events.py": """
            from __future__ import annotations

            from collections.abc import Callable
            from dataclasses import dataclass
            from threading import RLock


            @dataclass(frozen=True)
            class ProfileChanged:
                user_id: str
                revision: int


            class EventBus:
                def __init__(self) -> None:
                    self._lock = RLock()
                    self._subscribers: list[Callable[[ProfileChanged], None]] = []

                def subscribe(self, callback: Callable[[ProfileChanged], None]) -> None:
                    with self._lock:
                        self._subscribers.append(callback)

                def publish(self, event: ProfileChanged) -> None:
                    with self._lock:
                        subscribers = list(self._subscribers)
                    for callback in subscribers:
                        callback(event)
        """,
        "src/profiledirectory/service.py": """
            from __future__ import annotations

            from .cache import ProfileCache
            from .events import EventBus, ProfileChanged
            from .models import Profile, clean
            from .repository import ProfileRepository


            class DirectoryService:
                def __init__(
                    self,
                    repository: ProfileRepository | None = None,
                    cache: ProfileCache | None = None,
                    events: EventBus | None = None,
                ) -> None:
                    self.repository = repository if repository is not None else ProfileRepository()
                    self.cache = cache if cache is not None else ProfileCache()
                    self.events = events if events is not None else EventBus()
                    self.events.subscribe(self._on_profile_changed)

                def _on_profile_changed(self, event: ProfileChanged) -> None:
                    self.cache.invalidate(event.user_id)

                def get_user(self, tenant_id: str, user_id: str) -> dict[str, object] | None:
                    tenant_id = clean(tenant_id, "tenant_id")
                    user_id = clean(user_id, "user_id")
                    hit, profile = self.cache.lookup(tenant_id, user_id)
                    if not hit:
                        profile = self.repository.get(tenant_id, user_id)
                        self.cache.put(tenant_id, user_id, profile)
                    return None if profile is None else profile.as_dict()

                def create_user(
                    self,
                    tenant_id: str,
                    user_id: str,
                    email: str,
                    display_name: str,
                ) -> dict[str, object]:
                    profile = Profile(
                        clean(tenant_id, "tenant_id"),
                        clean(user_id, "user_id"),
                        clean(email, "email"),
                        clean(display_name, "display_name"),
                    )
                    self.repository.put(profile, expected_revision=0)
                    self.events.publish(ProfileChanged(profile.user_id, profile.revision))
                    return profile.as_dict()

                def update_user(
                    self,
                    tenant_id: str,
                    user_id: str,
                    *,
                    email: str | None = None,
                    display_name: str | None = None,
                ) -> dict[str, object]:
                    updated = self.repository.update(
                        clean(tenant_id, "tenant_id"),
                        clean(user_id, "user_id"),
                        email=email,
                        display_name=display_name,
                    )
                    self.events.publish(ProfileChanged(updated.user_id, updated.revision))
                    return updated.as_dict()

                def cache_size(self) -> int:
                    return self.cache.size()
        """,
        "src/profiledirectory/batch.py": """
            from __future__ import annotations

            from .service import DirectoryService


            def update_many(
                service: DirectoryService,
                tenant_id: str,
                changes: list[dict[str, str]],
            ) -> list[dict[str, object]]:
                results = []
                for change in changes:
                    results.append(
                        service.update_user(
                            tenant_id,
                            change["user_id"],
                            email=change.get("email"),
                            display_name=change.get("display_name"),
                        )
                    )
                return results
        """,
        "src/profiledirectory/query.py": """
            from __future__ import annotations

            from .models import Profile
            from .repository import ProfileRepository


            def list_tenant(repository: ProfileRepository, tenant_id: str) -> list[dict[str, object]]:
                profiles = [
                    profile
                    for profile in repository.snapshot()
                    if profile.tenant_id == tenant_id
                ]
                profiles.sort(key=lambda profile: profile.user_id)
                return [profile.as_dict() for profile in profiles]


            def revisions(repository: ProfileRepository, tenant_id: str) -> dict[str, int]:
                return {
                    profile.user_id: profile.revision
                    for profile in repository.snapshot()
                    if profile.tenant_id == tenant_id
                }


            def find_email(repository: ProfileRepository, tenant_id: str, email: str) -> Profile | None:
                matches = [
                    profile
                    for profile in repository.snapshot()
                    if profile.tenant_id == tenant_id and profile.email == email
                ]
                if len(matches) > 1:
                    raise ValueError("email is not unique within tenant")
                return matches[0] if matches else None
        """,
        "src/profiledirectory/coherence.py": """
            from __future__ import annotations

            from .service import DirectoryService


            def check_cached_users(
                service: DirectoryService,
                keys: list[tuple[str, str]],
            ) -> dict[str, object]:
                mismatches = []
                for tenant_id, user_id in keys:
                    cached = service.get_user(tenant_id, user_id)
                    stored = service.repository.get(tenant_id, user_id)
                    stored_value = None if stored is None else stored.as_dict()
                    if cached != stored_value:
                        mismatches.append({"tenant_id": tenant_id, "user_id": user_id})
                return {
                    "checked": len(keys),
                    "mismatches": mismatches,
                    "coherent": not mismatches,
                }
        """,
        "tests/test_directory.py": """
            import sys
            import unittest
            from pathlib import Path

            sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

            from profiledirectory import DirectoryService, Profile
            from profiledirectory.repository import ProfileRepository


            class DirectoryTests(unittest.TestCase):
                def make_service(self):
                    return DirectoryService(ProfileRepository([Profile("t", "u", "a@test", "A")]))

                def test_cold_read(self):
                    self.assertEqual(self.make_service().get_user("t", "u")["email"], "a@test")

                def test_update_returns_new_value(self):
                    service = self.make_service()
                    self.assertEqual(service.update_user("t", "u", email="b@test")["email"], "b@test")

                def test_result_is_defensive(self):
                    service = self.make_service()
                    result = service.get_user("t", "u")
                    result["email"] = "changed"
                    self.assertEqual(service.get_user("t", "u")["email"], "a@test")


            if __name__ == "__main__":
                unittest.main()
        """,
    }


def test_006() -> dict[str, str]:
    return {
        "README.md": """
            # SegmentStore

            Durable generation-based record store using checksummed binary
            segments and an atomically replaced JSON manifest.
        """,
        "pyproject.toml": project("segmentstore", "1.5.0"),
        "src/segmentstore/__init__.py": """
            from .errors import StoreCorruption
            from .store import SegmentStore

            __all__ = ["SegmentStore", "StoreCorruption"]
        """,
        "src/segmentstore/errors.py": """
            class StoreCorruption(ValueError):
                def __init__(self, path: str, offset: int, message: str) -> None:
                    super().__init__(f"{path} at byte {offset}: {message}")
                    self.path = path
                    self.offset = offset
        """,
        "src/segmentstore/format.py": """
            from __future__ import annotations

            import json
            import struct
            import zlib


            LENGTH = struct.Struct(">I")
            CHECKSUM = struct.Struct(">I")
            MAX_RECORD_BYTES = 4 * 1024 * 1024


            def encode_record(record: dict[str, object]) -> bytes:
                payload = json.dumps(
                    record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
                if len(payload) > MAX_RECORD_BYTES:
                    raise ValueError("record is too large")
                checksum = zlib.crc32(payload) & 0xFFFFFFFF
                return LENGTH.pack(len(payload)) + payload + CHECKSUM.pack(checksum)


            def decode_payload(payload: bytes, path: str, offset: int) -> dict[str, object]:
                try:
                    value = json.loads(payload.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    from .errors import StoreCorruption

                    raise StoreCorruption(path, offset, "invalid record JSON") from exc
                if not isinstance(value, dict):
                    from .errors import StoreCorruption

                    raise StoreCorruption(path, offset, "record is not an object")
                return value
        """,
        "src/segmentstore/reader.py": """
            from __future__ import annotations

            import zlib
            from pathlib import Path

            from .errors import StoreCorruption
            from .format import CHECKSUM, LENGTH, MAX_RECORD_BYTES, decode_payload


            def read_segment(path: Path) -> list[dict[str, object]]:
                if not path.exists():
                    raise StoreCorruption(str(path), 0, "segment is missing")
                data = path.read_bytes()
                records: list[dict[str, object]] = []
                offset = 0
                while offset < len(data):
                    record_offset = offset
                    if len(data) - offset < LENGTH.size:
                        raise StoreCorruption(str(path), offset, "truncated length prefix")
                    length = LENGTH.unpack_from(data, offset)[0]
                    offset += LENGTH.size
                    if length > MAX_RECORD_BYTES:
                        raise StoreCorruption(str(path), record_offset, "record length exceeds limit")
                    if len(data) - offset < length + CHECKSUM.size:
                        raise StoreCorruption(str(path), record_offset, "truncated final record")
                    payload = data[offset : offset + length]
                    offset += length
                    expected = CHECKSUM.unpack_from(data, offset)[0]
                    offset += CHECKSUM.size
                    actual = zlib.crc32(payload) & 0xFFFFFFFF
                    if actual != expected:
                        raise StoreCorruption(str(path), record_offset, "checksum mismatch")
                    records.append(decode_payload(payload, str(path), record_offset))
                return records
        """,
        "src/segmentstore/manifest.py": """
            from __future__ import annotations

            import json
            import os
            from pathlib import Path

            from .errors import StoreCorruption


            def manifest_path(root: Path) -> Path:
                return root / "MANIFEST.json"


            def next_manifest_path(root: Path) -> Path:
                return root / "MANIFEST.next"


            def load_manifest(path: Path) -> dict[str, object]:
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise StoreCorruption(str(path), 0, "invalid manifest") from exc
                if not isinstance(value, dict):
                    raise StoreCorruption(str(path), 0, "manifest is not an object")
                generation = value.get("generation")
                segments = value.get("segments")
                if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
                    raise StoreCorruption(str(path), 0, "invalid generation")
                if not isinstance(segments, list) or any(not isinstance(item, str) for item in segments):
                    raise StoreCorruption(str(path), 0, "invalid segment list")
                return {"generation": generation, "segments": list(segments)}


            def write_manifest(path: Path, value: dict[str, object]) -> None:
                payload = (
                    json.dumps(value, sort_keys=True, separators=(",", ":")) + "\\n"
                ).encode("utf-8")
                with path.open("wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())


            def install_manifest(root: Path, value: dict[str, object]) -> None:
                temporary = next_manifest_path(root)
                write_manifest(temporary, value)
                os.replace(temporary, manifest_path(root))
        """,
        "src/segmentstore/store.py": """
            from __future__ import annotations

            import os
            from pathlib import Path

            from .format import encode_record
            from .manifest import install_manifest, load_manifest, manifest_path, next_manifest_path
            from .reader import read_segment


            class SegmentStore:
                def __init__(self, root: str | Path) -> None:
                    self.root = Path(root)

                def initialize(self) -> None:
                    self.root.mkdir(parents=True, exist_ok=True)
                    if not manifest_path(self.root).exists():
                        segment = self.root / "segment-000000.bin"
                        segment.touch()
                        install_manifest(self.root, {"generation": 0, "segments": [segment.name]})

                def _manifest(self) -> dict[str, object]:
                    self.initialize()
                    return load_manifest(manifest_path(self.root))

                def append(self, record: dict[str, object]) -> None:
                    manifest = self._manifest()
                    segment = self.root / str(manifest["segments"][-1])
                    with segment.open("ab") as handle:
                        handle.write(encode_record(record))
                        handle.flush()
                        os.fsync(handle.fileno())

                def load(self) -> list[dict[str, object]]:
                    manifest = self._manifest()
                    records: list[dict[str, object]] = []
                    for name in manifest["segments"]:
                        records.extend(read_segment(self.root / str(name)))
                    return records

                def compact(self) -> None:
                    records = self.load()
                    current = self._manifest()
                    generation = int(current["generation"]) + 1
                    final_name = f"segment-{generation:06d}.bin"
                    temporary = self.root / (final_name + ".tmp")
                    with temporary.open("wb") as handle:
                        for record in records:
                            handle.write(encode_record(record))
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, self.root / final_name)
                    install_manifest(
                        self.root,
                        {"generation": generation, "segments": [final_name]},
                    )

                def recover(self) -> str:
                    pending = next_manifest_path(self.root)
                    if pending.exists():
                        pending.unlink()
                        return "rolled_back"
                    return "clean"
        """,
        "src/segmentstore/inspect.py": """
            from __future__ import annotations

            from pathlib import Path

            from .manifest import load_manifest, manifest_path, next_manifest_path


            def inspect_layout(root: str | Path) -> dict[str, object]:
                root = Path(root)
                active = manifest_path(root)
                pending = next_manifest_path(root)
                return {
                    "active": load_manifest(active) if active.exists() else None,
                    "pending": load_manifest(pending) if pending.exists() else None,
                    "files": sorted(path.name for path in root.iterdir()) if root.exists() else [],
                }
        """,
        "src/segmentstore/recovery_cli.py": """
            from __future__ import annotations

            import argparse
            import json
            from pathlib import Path

            from .inspect import inspect_layout
            from .store import SegmentStore


            def main(argv: list[str] | None = None) -> int:
                parser = argparse.ArgumentParser()
                parser.add_argument("--root", required=True, type=Path)
                parser.add_argument("mode", choices=["recover", "inspect"])
                args = parser.parse_args(argv)
                if args.mode == "recover":
                    result = {"outcome": SegmentStore(args.root).recover()}
                else:
                    result = inspect_layout(args.root)
                print(json.dumps(result, sort_keys=True))
                return 0


            if __name__ == "__main__":
                raise SystemExit(main())
        """,
        "src/segmentstore/writer.py": """
            from __future__ import annotations

            import os
            from pathlib import Path

            from .format import encode_record


            def append_record(path: Path, record: dict[str, object]) -> int:
                payload = encode_record(record)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("ab") as handle:
                    offset = handle.tell()
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                return offset


            def write_segment(path: Path, records: list[dict[str, object]]) -> int:
                count = 0
                with path.open("wb") as handle:
                    for record in records:
                        handle.write(encode_record(record))
                        count += 1
                    handle.flush()
                    os.fsync(handle.fileno())
                return count
        """,
        "src/segmentstore/recovery.py": """
            from __future__ import annotations

            import os
            from pathlib import Path

            from .manifest import load_manifest, manifest_path, next_manifest_path
            from .reader import read_segment


            def validate_manifest_segments(root: Path, manifest: dict[str, object]) -> None:
                for name in manifest["segments"]:
                    path = root / str(name)
                    read_segment(path)


            def choose_recovery(root: Path) -> str:
                active_path = manifest_path(root)
                pending_path = next_manifest_path(root)
                if not pending_path.exists():
                    validate_manifest_segments(root, load_manifest(active_path))
                    return "clean"
                pending = load_manifest(pending_path)
                try:
                    validate_manifest_segments(root, pending)
                except Exception:
                    pending_path.unlink()
                    return "rolled_back"
                os.replace(pending_path, active_path)
                return "committed"


            def remove_stale_temporaries(root: Path) -> list[str]:
                removed = []
                for path in sorted(root.glob("segment-*.bin.tmp")):
                    path.unlink()
                    removed.append(path.name)
                return removed
        """,
        "tests/test_store.py": """
            import tempfile
            import sys
            import unittest
            from pathlib import Path

            sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

            from segmentstore import SegmentStore


            class StoreTests(unittest.TestCase):
                def test_append_and_load(self):
                    with tempfile.TemporaryDirectory() as tmp:
                        store = SegmentStore(tmp)
                        store.append({"seq": 1})
                        store.append({"seq": 2})
                        self.assertEqual(store.load(), [{"seq": 1}, {"seq": 2}])

                def test_compaction_preserves_records(self):
                    with tempfile.TemporaryDirectory() as tmp:
                        store = SegmentStore(tmp)
                        store.append({"seq": 1})
                        store.compact()
                        self.assertEqual(store.load(), [{"seq": 1}])

                def test_clean_recovery(self):
                    with tempfile.TemporaryDirectory() as tmp:
                        store = SegmentStore(tmp)
                        store.initialize()
                        self.assertEqual(store.recover(), "clean")


            if __name__ == "__main__":
                unittest.main()
        """,
    }


CASES = {
    ("dev_cases", "dev_001"): dev_001,
    ("dev_cases", "dev_002"): dev_002,
    ("test_cases", "test_001"): test_001,
    ("test_cases", "test_002"): test_002,
    ("test_cases", "test_003"): test_003,
    ("test_cases", "test_004"): test_004,
    ("test_cases", "test_005"): test_005,
    ("test_cases", "test_006"): test_006,
}


def main() -> int:
    for (group, case_id), factory in CASES.items():
        write_tree(group, case_id, factory())
    print(f"generated {len(CASES)} repository assets under {ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

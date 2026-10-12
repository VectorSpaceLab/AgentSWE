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

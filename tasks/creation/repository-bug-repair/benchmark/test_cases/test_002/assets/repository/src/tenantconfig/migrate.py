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

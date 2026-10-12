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

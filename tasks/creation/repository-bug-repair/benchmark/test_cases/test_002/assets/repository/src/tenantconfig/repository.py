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

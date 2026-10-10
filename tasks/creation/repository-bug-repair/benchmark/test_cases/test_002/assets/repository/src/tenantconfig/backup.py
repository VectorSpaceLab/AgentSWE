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

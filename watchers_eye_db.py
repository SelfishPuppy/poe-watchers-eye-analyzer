"""
SQLite schema management and utilities for the Watcher's Eye analyzer.

This module is responsible for creating the local database file,
providing safe connection helpers, and exporting completed batches so
they can be pushed to Google Drive / Sheets.
"""

import csv
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    run_mode TEXT NOT NULL,             -- 'single' or 'double'
    league TEXT NOT NULL,
    divine_rate REAL NOT NULL,
    started_at TEXT NOT NULL,           -- ISO-8601 timestamp
    finished_at TEXT                    -- populated when run completes
);

CREATE TABLE IF NOT EXISTS prices_single (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    mod_id TEXT NOT NULL,
    mod_name TEXT NOT NULL,
    avg_divine REAL NOT NULL,
    avg_chaos REAL NOT NULL,
    divine_to_chaos_rate REAL NOT NULL,
    listing_count INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE,
    UNIQUE (batch_id, mod_id, recorded_at)
);

CREATE TABLE IF NOT EXISTS prices_double (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    mod1_id TEXT NOT NULL,
    mod2_id TEXT NOT NULL,
    mod1_name TEXT NOT NULL,
    mod2_name TEXT NOT NULL,
    avg_divine REAL NOT NULL,
    avg_chaos REAL NOT NULL,
    divine_to_chaos_rate REAL NOT NULL,
    listing_count INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE,
    CHECK (mod1_id < mod2_id),
    UNIQUE (batch_id, mod1_id, mod2_id, recorded_at)
);

CREATE INDEX IF NOT EXISTS idx_prices_single_mod ON prices_single (mod_id);
CREATE INDEX IF NOT EXISTS idx_prices_double_mods ON prices_double (mod1_id, mod2_id);
CREATE INDEX IF NOT EXISTS idx_prices_single_batch ON prices_single (batch_id);
CREATE INDEX IF NOT EXISTS idx_prices_double_batch ON prices_double (batch_id);
"""


def ensure_database(db_path: Path) -> None:
    """
    Create the SQLite database file (if needed) and apply the schema.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file. The parent directory will be
        created automatically if it does not already exist.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_connection(db_path: Path, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
    """
    Context manager that yields a SQLite connection.

    Parameters
    ----------
    db_path:
        Path to the database file.
    read_only:
        When True, opens the database in read-only mode.
    """
    if read_only:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def clear_batch(conn: sqlite3.Connection, batch_id: str) -> None:
    """
    Remove an existing batch and all of its associated rows. This helper
    will be useful if a run needs to be restarted with the same
    identifier.
    """
    conn.execute("DELETE FROM prices_single WHERE batch_id = ?", (batch_id,))
    conn.execute("DELETE FROM prices_double WHERE batch_id = ?", (batch_id,))
    conn.execute("DELETE FROM batches WHERE batch_id = ?", (batch_id,))
    conn.commit()


def export_batch_to_csv(
    db_path: Path,
    batch_id: str,
    output_path: Path,
    mode: str,
) -> Path:
    """
    Export all rows for the specified batch and mode to a CSV file.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.
    batch_id:
        The batch identifier to export.
    output_path:
        Destination file path for the CSV output.
    mode:
        Either ``"single"`` or ``"double"``.

    Returns
    -------
    Path
        The path of the written CSV file.
    """
    mode = mode.lower()
    if mode not in {"single", "double"}:
        raise ValueError("mode must be 'single' or 'double'")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if mode == "single":
        columns = [
            "batch_id",
            "mod_id",
            "mod_name",
            "avg_divine",
            "avg_chaos",
            "divine_to_chaos_rate",
            "listing_count",
            "recorded_at",
        ]
        query = f"SELECT {', '.join(columns)} FROM prices_single WHERE batch_id = ? ORDER BY mod_id"
    else:
        columns = [
            "batch_id",
            "mod1_id",
            "mod2_id",
            "mod1_name",
            "mod2_name",
            "avg_divine",
            "avg_chaos",
            "divine_to_chaos_rate",
            "listing_count",
            "recorded_at",
        ]
        query = f"SELECT {', '.join(columns)} FROM prices_double WHERE batch_id = ? ORDER BY mod1_id, mod2_id"

    with get_connection(db_path, read_only=True) as conn:
        rows = conn.execute(query, (batch_id,)).fetchall()

    with output_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(columns)
        writer.writerows(rows)

    return output_path

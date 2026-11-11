import sqlite3
from pathlib import Path


class RecordStore:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self):
        with self.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS source_records (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS raw_events (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                run_id TEXT NOT NULL,
                authority TEXT NOT NULL,
                event_id TEXT NOT NULL,
                parcel TEXT NOT NULL,
                code TEXT NOT NULL,
                event_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                replaces TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE,
                ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS projections (
                parcel TEXT NOT NULL,
                mapping_version INTEGER NOT NULL,
                state TEXT NOT NULL,
                run_id TEXT NOT NULL,
                computed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY (parcel, mapping_version)
            )""")

    def count(self):
        with self.connect() as connection:
            row = connection.execute("SELECT count(*) AS total FROM source_records").fetchone()
            return row["total"]

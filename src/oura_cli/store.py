from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime

from .config import private_dir
from .errors import OuraError


def now_iso():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, profile):
        self.profile = profile
        private_dir(profile.path)
        p = profile.path / "data.sqlite3"
        if p.is_symlink():
            raise OuraError("unsafe_path", "Database must not be a symbolic link")
        self.db = sqlite3.connect(p, timeout=30)
        os.chmod(p, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA busy_timeout=30000;
        CREATE TABLE IF NOT EXISTS documents (
          endpoint TEXT NOT NULL, id TEXT NOT NULL, day TEXT, payload TEXT NOT NULL,
          fetched_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(endpoint,id));
        CREATE INDEX IF NOT EXISTS document_days ON documents(endpoint,day);
        CREATE TABLE IF NOT EXISTS sync_state (
          endpoint TEXT PRIMARY KEY, last_success TEXT, end_date TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS handled_events (id TEXT PRIMARY KEY, processed_at TEXT NOT NULL);
        PRAGMA user_version=1;
        """)

    def close(self):
        self.db.close()

    def _record(self, endpoint, row, fetched):
        stamp = row.get("timestamp") or row.get("start_datetime") or row.get("bedtime_start")
        day = row.get("day")
        if not day and stamp:
            try:
                day = (
                    datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    .astimezone(self.profile.timezone)
                    .date()
                    .isoformat()
                )
            except (ValueError, TypeError):
                raise OuraError("invalid_response", "Record has an invalid timestamp") from None
        if not day:
            day = None
        key = row.get("id")
        if not key:
            # Heart-rate / battery samples are naturally identified by time, including revisions.
            key = (
                str(stamp) if stamp else hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
            )
        self.db.execute(
            """INSERT INTO documents VALUES(?,?,?,?,?,0)
          ON CONFLICT(endpoint,id) DO UPDATE SET day=excluded.day,payload=excluded.payload,
          fetched_at=excluded.fetched_at,deleted=0""",
            (endpoint, str(key), day, json.dumps(row), fetched),
        )

    def ingest(self, endpoint, rows, end_date, start_date=None):
        fetched = now_iso()
        with self.db:
            if start_date:
                self.db.execute(
                    "UPDATE documents SET deleted=1 WHERE endpoint=? AND day>=? AND day<=?",
                    (endpoint, start_date, end_date),
                )
            for row in rows:
                self._record(endpoint, row, fetched)
            self.db.execute(
                """INSERT INTO sync_state VALUES(?,?,?,NULL)
              ON CONFLICT(endpoint) DO UPDATE SET last_success=excluded.last_success,
              end_date=excluded.end_date,error=NULL""",
                (endpoint, fetched, end_date),
            )
        return fetched

    def fail(self, endpoint, error):
        with self.db:
            self.db.execute(
                """INSERT INTO sync_state(endpoint,error) VALUES(?,?)
              ON CONFLICT(endpoint) DO UPDATE SET error=excluded.error""",
                (endpoint, json.dumps(error)),
            )

    def states(self):
        return {
            r["endpoint"]: {
                "last_success": r["last_success"],
                "end_date": r["end_date"],
                "error": json.loads(r["error"]) if r["error"] else None,
            }
            for r in self.db.execute("SELECT * FROM sync_state")
        }

    def read(self, endpoint, start=None, end=None):
        sql, args = "SELECT payload FROM documents WHERE endpoint=? AND deleted=0", [endpoint]
        if start:
            sql += " AND day>=?"
            args.append(start)
        if end:
            sql += " AND day<=?"
            args.append(end)
        sql += " ORDER BY day,id"
        return [json.loads(r[0]) for r in self.db.execute(sql, args)]

    def freshness(self, endpoint, start=None, end=None):
        sql = "SELECT MAX(day),MAX(fetched_at) FROM documents WHERE endpoint=? AND deleted=0"
        args = [endpoint]
        if start:
            sql += " AND day>=?"
            args.append(start)
        if end:
            sql += " AND day<=?"
            args.append(end)
        row = self.db.execute(sql, args).fetchone()
        latest, fetched = row
        today = datetime.now(self.profile.timezone).date().isoformat()
        state = self.states().get(endpoint, {})
        return {
            "latest_day": latest,
            "fetched_at": fetched,
            "last_success": state.get("last_success"),
            "stale": latest is None or latest < today,
            "last_error": state.get("error"),
        }

    def event_done(self, event_id):
        return self.db.execute("SELECT 1 FROM handled_events WHERE id=?", (event_id,)).fetchone() is not None

    def apply_event(self, event_id, endpoint, document_id, row):
        with self.db:
            if self.event_done(event_id):
                return
            if row is None:
                self.db.execute(
                    "UPDATE documents SET deleted=1,fetched_at=? WHERE endpoint=? AND id=?",
                    (now_iso(), endpoint, document_id),
                )
            else:
                self._record(endpoint, row, now_iso())
            self.db.execute("INSERT INTO handled_events VALUES(?,?)", (event_id, now_iso()))

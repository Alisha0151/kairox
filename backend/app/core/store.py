"""Append-only event store (the "tape" that makes time-travel possible).

Every observation Kairox receives -- request outcomes, spans, metrics, state/config
changes -- is one immutable row in ``events``. Incidents are stored alongside.

Backends
--------
* SQLite  (default, zero-setup, used by the test-suite)
* PostgreSQL (``DATABASE_URL=postgresql://...``, used by docker-compose; needs psycopg2)
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional


class Store:
    def __init__(self, url: Optional[str] = None):
        self.url = url or ":memory:"
        self.pg = self.url.startswith(("postgres://", "postgresql://"))
        self._lock = threading.RLock()
        if self.pg:
            import psycopg2  # type: ignore

            self._conn = psycopg2.connect(self.url)
            self._conn.autocommit = True
            self.ph = "%s"
            pk = "BIGSERIAL PRIMARY KEY"
        else:
            path = self.url.replace("sqlite:///", "") if self.url.startswith("sqlite") else self.url
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self.ph = "?"
            pk = "INTEGER PRIMARY KEY AUTOINCREMENT"
        self._init_schema(pk)

    # ------------------------------------------------------------------ plumbing
    def _exec(self, sql: str, params: Iterable[Any] = (), fetch: bool = False):
        sql = sql.replace("?", self.ph)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(sql, tuple(params))
            if fetch:
                rows = cur.fetchall()
                if self.pg:
                    cols = [d[0] for d in cur.description]
                    rows = [dict(zip(cols, r)) for r in rows]
                else:
                    rows = [dict(r) for r in rows]
                cur.close()
                return rows
            if not self.pg:
                self._conn.commit()
            last = getattr(cur, "lastrowid", None)
            cur.close()
            return last

    def _init_schema(self, pk: str) -> None:
        self._exec(
            f"""CREATE TABLE IF NOT EXISTS events (
                id {pk}, ts DOUBLE PRECISION NOT NULL, kind TEXT NOT NULL,
                service TEXT NOT NULL, trace_id TEXT, payload TEXT NOT NULL)"""
        )
        self._exec("CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts)")
        self._exec("CREATE INDEX IF NOT EXISTS ix_events_kind ON events(kind, service)")
        self._exec("CREATE INDEX IF NOT EXISTS ix_events_trace ON events(trace_id)")
        self._exec(
            f"""CREATE TABLE IF NOT EXISTS incidents (
                id {pk}, created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL,
                status TEXT NOT NULL, data TEXT NOT NULL)"""
        )

    # ------------------------------------------------------------------ events
    def add_event(self, kind: str, service: str, payload: dict, ts: Optional[float] = None,
                  trace_id: Optional[str] = None) -> int:
        ts = time.time() if ts is None else ts
        sql = "INSERT INTO events(ts, kind, service, trace_id, payload) VALUES (?,?,?,?,?)"
        params = (ts, kind, service, trace_id, json.dumps(payload))
        if self.pg:
            rows = self._exec(sql + " RETURNING id", params, fetch=True)
            return int(rows[0]["id"])
        return int(self._exec(sql, params))

    def events(self, kind: Optional[str] = None, service: Optional[str] = None,
               since: Optional[float] = None, until: Optional[float] = None,
               trace_id: Optional[str] = None, limit: Optional[int] = None,
               newest_first: bool = False) -> list[dict]:
        where, params = [], []
        for col, val, op in (("kind", kind, "="), ("service", service, "="), ("trace_id", trace_id, "="),
                             ("ts", since, ">="), ("ts", until, "<=")):
            if val is not None:
                where.append(f"{col} {op} ?")
                params.append(val)
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts " + ("DESC" if newest_first else "ASC") + ", id " + ("DESC" if newest_first else "ASC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        out = []
        for r in self._exec(sql, params, fetch=True):
            r["payload"] = json.loads(r["payload"])
            out.append(r)
        return out

    def count_events(self) -> int:
        return int(self._exec("SELECT COUNT(*) AS n FROM events", fetch=True)[0]["n"])

    # ------------------------------------------------------------------ incidents
    def create_incident(self, data: dict) -> int:
        now = time.time()
        sql = "INSERT INTO incidents(created, updated, status, data) VALUES (?,?,?,?)"
        params = (now, now, data.get("status", "detected"), json.dumps(data))
        if self.pg:
            return int(self._exec(sql + " RETURNING id", params, fetch=True)[0]["id"])
        return int(self._exec(sql, params))

    def save_incident(self, incident_id: int, data: dict) -> None:
        self._exec("UPDATE incidents SET updated=?, status=?, data=? WHERE id=?",
                   (time.time(), data.get("status", "detected"), json.dumps(data), incident_id))

    def get_incident(self, incident_id: int) -> Optional[dict]:
        rows = self._exec("SELECT * FROM incidents WHERE id=?", (incident_id,), fetch=True)
        return self._inc(rows[0]) if rows else None

    def list_incidents(self) -> list[dict]:
        return [self._inc(r) for r in self._exec("SELECT * FROM incidents ORDER BY id DESC", fetch=True)]

    @staticmethod
    def _inc(row: dict) -> dict:
        data = json.loads(row["data"])
        data.update(id=row["id"], created=row["created"], updated=row["updated"], status=row["status"])
        return data

    def reset(self) -> None:
        self._exec("DELETE FROM events")
        self._exec("DELETE FROM incidents")

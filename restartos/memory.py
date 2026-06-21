"""
restartos.memory
================
Persistent incident memory in Postgres. Lets the system answer:
"Has this asset hit this alarm before? How was it resolved? What confidence?"

Hook points (see orchestration.py):
  * recall_similar(funcloc, alarm) - called after asset is resolved; the prior
    outcomes are injected into the evidence graph as cited facts so the agent
    loop can weigh them.
  * persist_run(result) - called at the end of every run (ACT or ABSTAIN) so
    the next incident on this asset benefits.

Defensive by design: if POSTGRES_URL is missing or the DB is unreachable,
every method is a silent no-op so the orchestrator keeps running.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


DDL = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id     TEXT PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    asset_funcloc   TEXT,
    asset_model     TEXT,
    line            TEXT,
    alarm_code      TEXT,
    decision        TEXT NOT NULL,
    top_cause       TEXT,
    top_confidence  DOUBLE PRECISION,
    mttr_min        INTEGER,
    parts_used      JSONB,
    trace_summary   TEXT,
    framework       TEXT,
    payload         JSONB
);
CREATE INDEX IF NOT EXISTS ix_incidents_asset_alarm
  ON incidents (asset_funcloc, alarm_code, ts DESC);
"""


@dataclass
class PriorOutcome:
    incident_id: str
    ts: datetime
    decision: str
    top_cause: Optional[str]
    top_confidence: Optional[float]
    mttr_min: Optional[int]
    parts_used: list


class IncidentMemory:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self.dsn = dsn or os.getenv("POSTGRES_URL") or \
            "postgresql://restartos:restartos@localhost:5432/restartos"
        self.ok = False
        try:
            import psycopg  # noqa: F401
            self._psycopg = psycopg
            self._ensure_schema()
            self.ok = True
        except Exception:
            self._psycopg = None

    def available(self) -> bool:
        return self.ok

    def _conn(self):
        return self._psycopg.connect(self.dsn, connect_timeout=3)

    def _ensure_schema(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(DDL)
            conn.commit()

    # ----------------------------------------------------------------- #
    def recall_similar(self, funcloc: Optional[str], alarm_code: Optional[str],
                       limit: int = 3) -> list[PriorOutcome]:
        if not self.ok or not funcloc:
            return []
        try:
            with self._conn() as conn, conn.cursor() as cur:
                if alarm_code:
                    cur.execute(
                        "SELECT incident_id, ts, decision, top_cause, top_confidence, "
                        "       mttr_min, parts_used "
                        "FROM incidents WHERE asset_funcloc=%s AND alarm_code=%s "
                        "ORDER BY ts DESC LIMIT %s",
                        (funcloc, alarm_code, limit))
                else:
                    cur.execute(
                        "SELECT incident_id, ts, decision, top_cause, top_confidence, "
                        "       mttr_min, parts_used "
                        "FROM incidents WHERE asset_funcloc=%s "
                        "ORDER BY ts DESC LIMIT %s",
                        (funcloc, limit))
                rows = cur.fetchall()
        except Exception:
            return []
        return [PriorOutcome(
            incident_id=r[0], ts=r[1], decision=r[2], top_cause=r[3],
            top_confidence=float(r[4]) if r[4] is not None else None,
            mttr_min=r[5], parts_used=r[6] or []) for r in rows]

    # ----------------------------------------------------------------- #
    def persist_run(self, *, incident_id: str, asset_funcloc: Optional[str],
                    asset_model: Optional[str], line: Optional[str],
                    alarm_code: Optional[str], decision: str,
                    top_cause: Optional[str], top_confidence: Optional[float],
                    mttr_min: Optional[int], parts_used: list,
                    trace_summary: str, framework: str,
                    payload: Optional[dict] = None) -> bool:
        if not self.ok:
            return False
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO incidents (incident_id, ts, asset_funcloc, asset_model, "
                    "  line, alarm_code, decision, top_cause, top_confidence, mttr_min, "
                    "  parts_used, trace_summary, framework, payload) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (incident_id) DO UPDATE SET "
                    "  ts=EXCLUDED.ts, decision=EXCLUDED.decision, "
                    "  top_cause=EXCLUDED.top_cause, top_confidence=EXCLUDED.top_confidence, "
                    "  mttr_min=EXCLUDED.mttr_min, parts_used=EXCLUDED.parts_used, "
                    "  trace_summary=EXCLUDED.trace_summary, payload=EXCLUDED.payload",
                    (incident_id, datetime.now(timezone.utc), asset_funcloc, asset_model,
                     line, alarm_code, decision, top_cause, top_confidence, mttr_min,
                     json.dumps(parts_used or []), trace_summary, framework,
                     json.dumps(payload or {})))
                conn.commit()
            return True
        except Exception:
            return False

    # ----------------------------------------------------------------- #
    def stats(self) -> dict:
        if not self.ok:
            return {"available": False}
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*), COUNT(DISTINCT asset_funcloc) FROM incidents")
                total, distinct_assets = cur.fetchone()
            return {"available": True, "incidents": int(total),
                    "distinct_assets": int(distinct_assets), "dsn": self.dsn}
        except Exception:
            return {"available": True, "incidents": "?", "dsn": self.dsn}


def format_priors_as_facts(priors: list[PriorOutcome]) -> str:
    """Render prior outcomes as a single human/agent-readable fact string."""
    if not priors:
        return ""
    parts = []
    for p in priors:
        ts = p.ts.strftime("%Y-%m-%d") if isinstance(p.ts, datetime) else str(p.ts)
        conf = f" (conf {p.top_confidence:.2f})" if p.top_confidence is not None else ""
        mt = f", MTTR {p.mttr_min}min" if p.mttr_min else ""
        cause = p.top_cause or "no cause recorded"
        parts.append(f"{ts}: {p.decision} -> {cause}{conf}{mt}")
    return "PRIOR_INCIDENTS_ON_THIS_ASSET: " + " | ".join(parts)

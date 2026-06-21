"""
restartos.actions
================
The IT Action Plane (Purdue L4) — the ONLY place state changes, and only after
the gate. Every write is IDEMPOTENT (deterministic key derived from the incident
+ action) so a retry never double-creates a work order or double-reserves parts.

Writes are simulated against a local JSON "IT systems" store, but every call
first passes through the capability boundary: assert_capability(IT_BUSINESS, WRITE).
There is no method here that targets an OT plane.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from .domain import Access, Plane
from .security import assert_capability


@dataclass
class ActionResult:
    system: str
    action: str
    idempotency_key: str
    created: bool
    record: dict


class ITActionPlane:
    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self._store_path = os.path.join(state_dir, "it_state.json")
        self.store = json.load(open(self._store_path)) if os.path.exists(self._store_path) else {}

    def _key(self, *parts: str) -> str:
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]

    def _commit(self, system: str, action: str, key: str, record: dict) -> ActionResult:
        assert_capability(Plane.IT_BUSINESS, Access.WRITE)   # boundary enforcement
        bucket = self.store.setdefault(system, {})
        if key in bucket:
            return ActionResult(system, action, key, False, bucket[key])  # idempotent hit
        bucket[key] = record
        json.dump(self.store, open(self._store_path, "w"), indent=2)
        return ActionResult(system, action, key, True, record)

    def create_work_order(self, incident_id: str, funcloc: str, plan: dict) -> ActionResult:
        key = self._key("WO", incident_id, funcloc)
        rec = {"wo_id": f"WO-AUTO-{key}", "funcloc": funcloc, "status": "DRAFT",
               "cause": plan.get("cause"), "iso14224": plan.get("iso"),
               "est_minutes": plan.get("est_minutes"), "steps": plan.get("steps")}
        return self._commit("CMMS", "create_work_order", key, rec)

    def reserve_parts(self, incident_id: str, parts: list[dict]) -> ActionResult:
        key = self._key("PARTS", incident_id, ",".join(p["part_no"] for p in parts))
        rec = {"reservation_id": f"RES-{key}", "lines": parts, "status": "RESERVED"}
        return self._commit("ERP", "reserve_parts", key, rec)

    def create_qc_plan(self, incident_id: str, funcloc: str) -> ActionResult:
        key = self._key("QC", incident_id, funcloc)
        rec = {"plan_id": f"QCP-{key}", "funcloc": funcloc,
               "sampling": "AQL 2.5, 5 units post-restart, check fill volume"}
        return self._commit("QMS", "create_qc_plan", key, rec)

    def notify(self, incident_id: str, who: str, message: str) -> ActionResult:
        key = self._key("NOTIFY", incident_id, who, message[:24])
        return self._commit("NOTIFY", "notify", key,
                            {"to": who, "message": message, "status": "SENT"})

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
    """The single chokepoint for every IT write.

    When `cmms_backend`, `parts_backend`, or `notifier` are provided (built
    from `connectors.build_cmms`/`build_parts_backend`/`build_notifier` when
    RESTARTOS_LIVE=1), the work order, parts reservation, and notification
    are issued against the REAL plant systems over HTTPS.

    When the live backends are None, the same calls fall back to a local
    JSON store. Either way: idempotent by SHA1 of (incident, action, args),
    so a retry never double-creates anything.
    """

    def __init__(self, state_dir: str,
                 cmms_backend=None, parts_backend=None, notifier=None) -> None:
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self._store_path = os.path.join(state_dir, "it_state.json")
        self.store = json.load(open(self._store_path)) if os.path.exists(self._store_path) else {}
        # Live backends (any may be None → that destination uses JSON fallback)
        self.cmms = cmms_backend
        self.parts = parts_backend
        self.notifier = notifier

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
        # Live CMMS path
        if self.cmms is not None:
            try:
                description = f"{plan.get('cause','Recovery')}: " + \
                              "; ".join((plan.get('steps') or [])[:3])
                # Both Fiix and Maximo clients expose create_work_order(...) with
                # similar shapes; the call signature here matches both.
                resp = self.cmms.create_work_order(funcloc, description, key)
                rec = {
                    "wo_id": resp.get("ID") or resp.get("wonum") or f"WO-LIVE-{key}",
                    "funcloc": funcloc, "status": "Open",
                    "cause": plan.get("cause"), "iso14224": plan.get("iso"),
                    "est_minutes": plan.get("est_minutes"),
                    "steps": plan.get("steps"),
                    "_backend": type(self.cmms).__name__,
                    "_raw": resp,
                }
                return self._commit("CMMS", "create_work_order", key, rec)
            except Exception as e:
                # Real CMMS rejected — capture the error in the audit trail and
                # fall back to the JSON store so the engine can still hand the
                # operator a draft for manual filing.
                rec = {"wo_id": f"WO-FALLBACK-{key}", "funcloc": funcloc,
                       "status": "DRAFT_LIVE_FAILED", "cause": plan.get("cause"),
                       "iso14224": plan.get("iso"), "est_minutes": plan.get("est_minutes"),
                       "steps": plan.get("steps"), "_live_error": str(e)}
                return self._commit("CMMS", "create_work_order", key, rec)
        # Simulated path
        rec = {"wo_id": f"WO-AUTO-{key}", "funcloc": funcloc, "status": "DRAFT",
               "cause": plan.get("cause"), "iso14224": plan.get("iso"),
               "est_minutes": plan.get("est_minutes"), "steps": plan.get("steps")}
        return self._commit("CMMS", "create_work_order", key, rec)

    def reserve_parts(self, incident_id: str, parts: list[dict]) -> ActionResult:
        key = self._key("PARTS", incident_id, ",".join(p["part_no"] for p in parts))
        if self.parts is not None:
            try:
                # We need a work order id to attach; look up the just-committed WO.
                wo_bucket = self.store.get("CMMS", {})
                wo_id = next((r["wo_id"] for r in wo_bucket.values()
                              if r.get("_live_error") is None), None)
                resp = self.parts.reserve(wo_id or incident_id, parts, key)
                rec = {"reservation_id": resp.get("ID") or f"RES-LIVE-{key}",
                       "lines": parts, "status": "RESERVED",
                       "_backend": type(self.parts).__name__, "_raw": resp}
                return self._commit("ERP", "reserve_parts", key, rec)
            except Exception as e:
                rec = {"reservation_id": f"RES-FALLBACK-{key}",
                       "lines": parts, "status": "RESERVE_LIVE_FAILED",
                       "_live_error": str(e)}
                return self._commit("ERP", "reserve_parts", key, rec)
        rec = {"reservation_id": f"RES-{key}", "lines": parts, "status": "RESERVED"}
        return self._commit("ERP", "reserve_parts", key, rec)

    def create_qc_plan(self, incident_id: str, funcloc: str) -> ActionResult:
        key = self._key("QC", incident_id, funcloc)
        rec = {"plan_id": f"QCP-{key}", "funcloc": funcloc,
               "sampling": "AQL 2.5, 5 units post-restart, check fill volume"}
        return self._commit("QMS", "create_qc_plan", key, rec)

    def notify(self, incident_id: str, who: str, message: str) -> ActionResult:
        key = self._key("NOTIFY", incident_id, who, message[:24])
        # Live notifier path (Slack webhook)
        if self.notifier is not None:
            try:
                resp = self.notifier.notify(who, message)
                rec = {"to": who, "message": message,
                       "status": "SENT" if resp.get("ok") else "FAILED",
                       "_backend": type(self.notifier).__name__, "_raw": resp}
                return self._commit("NOTIFY", "notify", key, rec)
            except Exception as e:
                rec = {"to": who, "message": message, "status": "SEND_FAILED",
                       "_live_error": str(e)}
                return self._commit("NOTIFY", "notify", key, rec)
        return self._commit("NOTIFY", "notify", key,
                            {"to": who, "message": message, "status": "SENT"})

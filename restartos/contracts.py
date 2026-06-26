"""
restartos.contracts
====================
Two trust-building outputs the engine attaches to EVERY run:

  * the Decision Contract — a single, plain statement of what the agent decided,
    what it is ALLOWED to do next, what it is BLOCKED from doing by construction
    (any PLC / SCADA / OT write), what IT-side actions a human may approve, the
    evidence it used, the evidence still missing, the risk class, and the audit
    id; and
  * the Escalation Packet — the productive output of a blocked run. A safe
    refusal that doesn't tell the next person what to do is worthless on a shop
    floor, so a blocked run still hands over what was checked, what was missing,
    what blocked it, the likely cause, who to route to, and the exact next human
    step.

It also holds the NEED_MORE_INFO probe: instead of only choosing ACT vs ABSTAIN,
the engine can ask for the ONE missing input that would unblock a decision.
"""
from __future__ import annotations

from typing import Optional

from .domain import (Decision, DecisionContract, EscalationPacket,
                     MissingEvidenceRequest)


# Blocked by construction — these never become an allowed action, regardless of
# confidence or approval. The OT/IT boundary is enforced in security.py; this is
# the human-readable contract that mirrors it.
FORBIDDEN_ACTIONS = [
    "Write to PLC / controller logic",
    "Write to SCADA / HMI setpoints",
    "Change any OT setpoint or recipe on the line",
    "Directly restart or command the line (no OT write path exists)",
]

# The only writes a human may approve — all IT-side business systems.
APPROVED_IT_ACTIONS = [
    "Create CMMS work order",
    "Reserve parts in ERP",
    "File QMS QC sampling plan",
    "Notify technician via Slack / Teams",
]


# The evidence a confident, safe recovery decision should rest on, with the
# weight each source carries toward a 100-point sufficiency score.
SUFFICIENCY_SOURCES = [
    ("HISTORIAN", "Machine event timeline", 18),
    ("MANUAL", "OEM procedure", 15),
    ("SAFETY", "Safety / LOTO", 15),
    ("CMMS", "Maintenance history", 15),
    ("PARTS", "Parts availability", 10),
    ("HR", "Qualified technician", 10),
    ("MES", "Production / downtime", 5),
    ("SHIFT_NOTES", "Shift notes", 5),
    ("MOC", "Recent changes", 5),
    ("QUALITY", "Quality rule", 2),   # always partial — QC plan is generated, not gathered
]


def evidence_sufficiency(ctx) -> dict:
    """Score whether the agent gathered ENOUGH to decide — not how confident it
    feels. Manufacturing decisions need evidence, not confidence vibes."""
    present = {e.source_system for e in ctx.graph.items.values()}
    items, score = [], 0.0
    for sys, label, weight in SUFFICIENCY_SOURCES:
        if sys == "QUALITY":
            status, got = "partial", weight * 0.5      # QC plan exists but isn't gathered evidence
        elif sys in present:
            status, got = "present", weight
        else:
            status, got = "missing", 0.0
        score += got
        items.append({"system": sys, "label": label, "status": status})
    missing = [i["label"] for i in items if i["status"] == "missing"]
    return {"score": int(round(score)), "items": items, "missing": missing}


def build_missing_evidence_request(ctx, top) -> Optional[MissingEvidenceRequest]:
    """If a single, specific human-supplied input would plausibly unblock the
    decision, return the ask. Otherwise None (→ a plain ABSTAIN)."""
    inc = ctx.incident
    has_historian = any(e.source_system == "HISTORIAN" for e in ctx.graph.items.values())

    # 1) No controller alarm code — the single most common real gap. Anchors the
    #    manual lookup and the differential.
    if not inc.alarm_code:
        return MissingEvidenceRequest(
            item="controller alarm code / HMI alarm screen",
            why="No alarm code was given, so the manual lookup and the differential "
                "cannot be firmly anchored.",
            how_to_provide="Read the alarm code off the HMI, or upload a photo of "
                           "the HMI alarm screen.",
            unblocks="manual procedure retrieval and a grounded diagnosis")

    # 2) Sensor history unavailable — can't confirm the fault signature.
    if not has_historian:
        return MissingEvidenceRequest(
            item="current sensor readings (head pressure / flow)",
            why="Historian readings are unavailable, so the fault signature cannot "
                "be confirmed from live data.",
            how_to_provide="Confirm head-pressure and flow from the HMI trend screen.",
            unblocks="confirmation of the fault signature")

    # 3) Borderline confidence with a clogged-signature lead but no manual hit:
    #    a quick human check of the alarm screen would tip it.
    has_manual = any(e.source_system == "MANUAL" for e in ctx.graph.items.values())
    if not has_manual and top is not None and getattr(top, "confidence", 0) >= 0.45:
        return MissingEvidenceRequest(
            item="OEM manual section for this alarm",
            why="The cited procedure could not be retrieved, so the plan cannot be "
                "grounded to a manual passage.",
            how_to_provide="Confirm the OEM model number, or point to the manual "
                           "section for this alarm.",
            unblocks="a manual-grounded, verifiable recovery plan")

    return None


def build_decision_contract(incident_id: str, decision: Decision,
                            risk_class: str, audit_id: str, confidence: float,
                            evidence_used: list[str], missing_evidence: list[str],
                            route_to: str = "") -> DecisionContract:
    if decision == Decision.ACT:
        allowed = "Create the recovery work package (pending human approval)"
        note = "Confident, cross-model verified, safety-checked → routed to the gate."
    elif decision == Decision.NEED_MORE_INFO:
        item = missing_evidence[0] if missing_evidence else "the missing input"
        allowed = f"Collect missing evidence: {item}"
        note = "One specific input would unblock a decision; not enough to act yet."
    else:  # ABSTAIN
        allowed = f"Escalate to {route_to or 'senior maintenance'}"
        note = "Could not ground/verify a safe plan → safe refusal with escalation."
    return DecisionContract(
        incident_id=incident_id, decision=decision, allowed_next_action=allowed,
        human_approval_required=True, forbidden_actions=list(FORBIDDEN_ACTIONS),
        approved_it_actions=list(APPROVED_IT_ACTIONS),
        evidence_used=evidence_used, missing_evidence=missing_evidence,
        risk_class=risk_class, audit_id=audit_id, confidence=round(confidence, 3),
        note=note)


def build_escalation_packet(ctx, blocking_reason: str, top,
                            missing: Optional[MissingEvidenceRequest],
                            route_to: str) -> EscalationPacket:
    funcloc = getattr(ctx.asset, "funcloc", None) or ctx.incident.asset_hint
    checked = sorted({e.source_system for e in ctx.graph.items.values()})
    missing_list: list[str] = []
    if missing:
        missing_list.append(missing.item)
    if not any(e.source_system == "HISTORIAN" for e in ctx.graph.items.values()):
        missing_list.append("live historian readings")
    contradictions = [c.note for c in ctx.graph.contradictions]
    if missing:
        next_step = missing.how_to_provide
    elif contradictions:
        next_step = ("Resolve the evidence contradiction before any restart: "
                     + contradictions[0])
    else:
        next_step = (f"Senior review of {funcloc}: confirm the fault on "
                     "the line, then re-run.")
    return EscalationPacket(
        incident_id=ctx.incident.incident_id,
        asset_funcloc=funcloc,
        operator_reported=(ctx.incident.raw_message
                           or f"{ctx.incident.asset_hint}: {ctx.incident.symptom} "
                              f"(alarm {ctx.incident.alarm_code})"),
        evidence_checked=checked,
        evidence_missing=sorted(set(missing_list)),
        blocking_reason=blocking_reason,
        likely_cause=getattr(top, "cause", None) if top else None,
        confidence=round(getattr(top, "confidence", 0.0), 3) if top else 0.0,
        route_to=route_to,
        next_human_step=next_step,
        contradictions=contradictions)

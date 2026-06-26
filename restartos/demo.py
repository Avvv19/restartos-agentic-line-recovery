"""
restartos.demo
==============
Five deterministic demo scenes that prove the system is an *agent* — it decides,
calls tools, produces an outcome, refuses when it should, and cannot touch OT.

Each scene returns the full run JSON the cockpit already knows how to render, so
the demo surface is just a thin presenter over real engine output. The engine is
deterministic offline (mock LLM), so the same scene yields the same result every
time — exactly what you want in front of judges.

  Scene 1  ACT                     — messy note → recovery package → approve → IT writes
  Scene 2  NEED_MORE_INFO          — one missing input → ask for exactly that
  Scene 3  ABSTAIN                 — weak/unknown evidence → escalation packet
  Scene 4  VERIFIER CATCHES IT     — planner cites a bad page → verifier refutes → re-plan
  Scene 5  OT WRITE BLOCKED        — attempt to actuate the line → blocked by construction
"""
from __future__ import annotations

import os

from .domain import GateOutcome, Incident, Severity, to_jsonable
from .intake import intake_to_incident, parse_message
from .orchestration import RestartOSEngine


def _approve(_d):
    return GateOutcome.APPROVED


# Static before/after framing — the pain, made impossible to miss.
BEFORE_AFTER = {
    "before": [
        "Line down — alarm on the HMI",
        "Tech opens the historian, reads the alarm signature",
        "Opens CMMS, scrolls the last 15-30 work orders",
        "Opens a 400-page OEM PDF, hunts for the right section",
        "Walks to the parts cage, checks stock",
        "Checks the shift roster for a certified tech",
        "Reads yesterday's handoff for tribal context",
        "Fills a 30-field work order, reserves parts, drafts a handoff",
        "45-90 minutes lost while the line bleeds $5k-$50k/hr",
    ],
    "after": [
        "Messy operator note pasted in",
        "Evidence gathered across 9 plant systems in seconds",
        "First actionable fault isolated from the downstream noise",
        "Recovery Work Package generated, cross-model verified, safety-checked",
        "Human reads one screen and approves",
        "CMMS work order + ERP parts + QMS QC plan + technician paged",
        "~30 seconds",
    ],
}


def ot_blocked() -> dict:
    """Scene 5 — prove no OT write path exists. This is not policy; the
    capability check raises by construction."""
    from .domain import Access, Plane
    from .security import OTWriteForbidden, assert_capability, writable_planes
    attempts = [
        ("write_plc_speed(Line 3, 60%)", Plane.OT_CONTROL),
        ("set_scada_setpoint(filler_pressure, 4.2bar)", Plane.OT_CONTROL),
        ("write_historian_tag(4471, 0)", Plane.OT_OPS),
    ]
    out = []
    for label, plane in attempts:
        try:
            assert_capability(plane, Access.WRITE)
            out.append({"attempt": label, "plane": plane.value,
                        "result": "ALLOWED", "blocked": False})
        except OTWriteForbidden as e:
            out.append({"attempt": label, "plane": plane.value,
                        "result": "BLOCKED", "blocked": True, "reason": str(e)})
    return {"kind": "ot_block", "id": 5,
            "title": "OT write blocked by construction",
            "tagline": "The agent can recommend a restart. It cannot perform one.",
            "writable_planes": writable_planes(), "attempts": out,
            "summary": "Every OT write path raises OTWriteForbidden. Restart and "
                       "LOTO are human-only. The agent writes IT systems only, "
                       "after approval."}


# Scene definitions for the run-based scenes (1-4).
_RUN_SCENES = [
    {"id": 1, "kind": "run", "decision": "ACT",
     "title": "Successful recovery",
     "tagline": "Messy note → first fault → verified package → approve → IT writes",
     "message": "Line 3 filler keeps stopping after 20 min, bottles backing up at "
                "the capper, A-220, need a restart before 3 PM",
     "auto_approve": True, "replan": False},
    {"id": 2, "kind": "run", "decision": "NEED_MORE_INFO",
     "title": "Need more info",
     "tagline": "Close, but one input is missing — ask for exactly that",
     "message": "the filler keeps stopping and bottles are backing up at the capper",
     "auto_approve": True, "replan": False},
    {"id": 3, "kind": "run", "decision": "ABSTAIN",
     "title": "Abstain + escalate",
     "tagline": "Weak/unknown evidence → refuse, hand over a useful packet",
     "incident": {"asset_hint": "ghost machine xyz", "symptom": "weird noise",
                  "line": "Line 9", "alarm": None},
     "auto_approve": False, "replan": False},
    {"id": 4, "kind": "run", "decision": "ACT (after self-correction)",
     "title": "Verifier catches a hallucination",
     "tagline": "Planner cites a non-existent page → verifier refutes → re-plan",
     "message": "Line 3 filler down, A-220, low flow and high head pressure",
     "auto_approve": True, "replan": True},
]


def run_scene(eng: RestartOSEngine, spec: dict) -> dict:
    """Execute one run-based scene and return a presenter-ready dict."""
    if "message" in spec:
        inc = intake_to_incident(parse_message(spec["message"]))
    else:
        i = spec["incident"]
        inc = Incident(asset_hint=i["asset_hint"], symptom=i["symptom"],
                       line=i["line"], alarm_code=i["alarm"],
                       severity=Severity(i.get("severity", "HIGH")))
    prev = os.environ.get("RESTARTOS_DEMO_REPLAN")
    if spec.get("replan"):
        os.environ["RESTARTOS_DEMO_REPLAN"] = "1"
    try:
        res = eng.run(inc, approver=_approve if spec.get("auto_approve") else None)
    finally:
        if spec.get("replan"):
            if prev is None:
                os.environ.pop("RESTARTOS_DEMO_REPLAN", None)
            else:
                os.environ["RESTARTOS_DEMO_REPLAN"] = prev
    run = to_jsonable(res.__dict__)
    return {"kind": "run", "id": spec["id"], "title": spec["title"],
            "tagline": spec["tagline"], "expected_decision": spec["decision"],
            "message": spec.get("message"), "run": run}


def build_all(eng: RestartOSEngine | None = None) -> dict:
    """Build every scene + the before/after framing. Deterministic offline."""
    eng = eng or RestartOSEngine()
    scenes = [run_scene(eng, s) for s in _RUN_SCENES]
    scenes.append(ot_blocked())
    manifest = [{"id": s["id"], "title": s["title"], "tagline": s["tagline"],
                 "kind": s["kind"],
                 "decision": s.get("expected_decision")
                 or s.get("run", {}).get("decision") or "BLOCKED"}
                for s in scenes]
    return {"before_after": BEFORE_AFTER, "manifest": manifest, "scenes": scenes}

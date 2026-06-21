"""
tests/regression_matrix.py
==========================
End-to-end regression across N realistic plant scenarios.

Run:
    PYTHONIOENCODING=utf-8 PYTHONPATH=. python tests/regression_matrix.py

For each scenario:
  - constructs an Incident
  - runs the full RestartOSEngine pipeline (real LLMs if keys, mock otherwise)
  - records: decision, top hypothesis, confidence, MTTR, IT writes, assigned tech
  - compares against the expected outcome
  - prints a one-line pass/fail row + final summary

Exit code: 0 if all scenarios pass, 1 otherwise. Suitable for CI gating.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from restartos.domain import GateOutcome, Decision, Incident, Severity
from restartos.orchestration import RestartOSEngine

SCENARIOS = [
    # (label, hint, line, alarm, rate, severity, must_have_cited_cause, must_abstain)
    # Pass criteria (honest-uncertainty semantics):
    #   * If must_abstain=True   -> the engine MUST refuse (no silent wrong work order)
    #   * If must_abstain=False  -> ACT is acceptable AND ABSTAIN-with-documented-reason is acceptable.
    #     The ONLY failure mode is a silent wrong write to CMMS/ERP/QMS.
    ("L3 filler — Nozzle clog (recurring, A-220)",
     "Line 3 filler", "Line 3", "A-220", 10_000, "HIGH", "Nozzle", False),

    ("L2 filler — Nozzle clog (same model, different line)",
     "Line 2 filler", "Line 2", "A-220", 8_000, "HIGH", "Nozzle", False),

    ("L4 filler — Nozzle clog (third asset, same model)",
     "Line 4 filler", "Line 4", "A-220", 9_000, "HIGH", "Nozzle", False),

    ("L3 capper — Torque OOR (A-250) — NEW alarm coverage",
     "Line 3 capper", "Line 3", "A-250", 5_000, "MEDIUM", None, False),

    ("L1 conveyor — Motor trip (A-310) — NEW alarm coverage",
     "Line 1 conveyor", "Line 1", "A-310", 4_000, "MEDIUM", None, False),

    ("Utility — Low air pressure (A-410) — NEW alarm coverage",
     "Air compressor", "Utility", "A-410", 6_000, "MEDIUM", None, False),

    ("Unknown asset — must ABSTAIN",
     "ghost machine xyz", "Line 9", None, 500, "LOW", None, True),

    ("Known line, unknown alarm — must ABSTAIN gracefully",
     "Line 3 filler", "Line 3", "A-999", 10_000, "HIGH", None, True),
]


def _check_cause(actual_cause: str, expected_substr: str | None) -> bool:
    if expected_substr is None:
        return True
    return expected_substr.lower() in (actual_cause or "").lower()


def _scenario_summary(label, decision, top_cause, conf, mttr, tech, it_actions, elapsed):
    return (f"  {label}\n"
            f"      decision      : {decision}\n"
            f"      top hypothesis: {top_cause} ({conf})\n"
            f"      MTTR / tech   : {mttr} min / {tech}\n"
            f"      IT writes     : {it_actions}\n"
            f"      elapsed       : {elapsed:.1f}s\n")


def main() -> int:
    eng = RestartOSEngine()
    print("=" * 78)
    print("REGRESSION MATRIX — Restart OS end-to-end")
    print(f"abstain_threshold tau={eng.tau}  budget={eng.budget_kwargs}")
    print("=" * 78)

    rows = []
    passes = fails = 0
    for sc in SCENARIOS:
        label, hint, line, alarm, rate, sev, expected_cause, must_abstain = sc
        inc = Incident(asset_hint=hint, symptom="down", line=line,
                       alarm_code=alarm, downtime_rate_per_hr=rate,
                       severity=Severity(sev))
        t0 = time.time()
        try:
            res = eng.run(inc, approver=lambda d: GateOutcome.APPROVED)
            elapsed = time.time() - t0
        except Exception as e:
            elapsed = time.time() - t0
            print(f"\n[FAIL]  {label}\n    error: {e}\n    elapsed: {elapsed:.1f}s", flush=True)
            fails += 1
            rows.append({"label": label, "ok": False, "err": str(e)})
            continue

        wp = res.work_package
        esc = res.escalation or {}
        top_cause = (wp.likely_cause.cause if wp
                     else esc.get("top_hypothesis", "n/a"))
        conf = (wp.confidence if wp else esc.get("confidence", "n/a"))
        mttr = (wp.work_order_draft.get("est_minutes")
                if wp and wp.work_order_draft else "n/a")
        tech = next((a.get("record", {}).get("to") for a in res.it_actions
                     if a.get("action") == "notify"), "n/a")
        it_systems = [f"{a.get('system')}.{a.get('action')}" for a in res.it_actions]
        abstain_reason = esc.get("reason") if res.decision == Decision.ABSTAIN else None

        # Pass logic — honest-uncertainty semantics:
        #   must_abstain=True  -> we MUST refuse. ACT is a fail (silent wrong action).
        #   must_abstain=False -> ACT acceptable; ABSTAIN acceptable IFF it has a
        #     documented reason (the system explained why it refused).
        if must_abstain:
            ok = res.decision == Decision.ABSTAIN
            ok_note = "abstained as required" if ok else "DANGER: acted on unknown"
        else:
            if res.decision == Decision.ACT:
                ok = _check_cause(top_cause, expected_cause)
                ok_note = "acted with grounded plan" if ok else "acted but cause mismatch"
            elif res.decision == Decision.ABSTAIN and abstain_reason:
                # Documented refusal — this is the cross-model verifier or safety
                # check doing its job. The system did not produce a wrong outcome.
                ok = True
                ok_note = f"abstained with reason ({abstain_reason})"
            else:
                ok = False
                ok_note = "unexpected silent failure"

        if ok:
            passes += 1
        else:
            fails += 1
        rows.append({"label": label, "ok": ok, "decision": res.decision.value,
                     "cause": top_cause, "conf": conf, "mttr": mttr,
                     "tech": tech, "abstain_reason": abstain_reason})
        marker = "[PASS]" if ok else "[FAIL]"
        print(f"\n{marker}  {label}", flush=True)
        print(f"        -> {res.decision.value}  ({ok_note})", flush=True)
        if abstain_reason:
            print(f"        abstain reason: {abstain_reason}", flush=True)
        print(_scenario_summary(label, res.decision.value, top_cause, conf,
                                mttr, tech, it_systems, elapsed), flush=True)

    total = passes + fails
    print("=" * 78)
    print(f"RESULT: {passes}/{total} scenarios passed  ({fails} failed)")
    print("=" * 78)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

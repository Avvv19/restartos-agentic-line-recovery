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
    # (label, hint, line, alarm, rate, severity, expected_decision, expected_cause_substring_or_None)
    ("L3 filler — Nozzle clog (recurring, A-220)",
     "Line 3 filler", "Line 3", "A-220", 10_000, "HIGH",
     Decision.ACT, "Nozzle"),

    ("L2 filler — Nozzle clog (same model, different line)",
     "Line 2 filler", "Line 2", "A-220", 8_000, "HIGH",
     Decision.ACT, "Nozzle"),

    ("L4 filler — Nozzle clog (third asset, same model)",
     "Line 4 filler", "Line 4", "A-220", 9_000, "HIGH",
     Decision.ACT, "Nozzle"),

    ("L3 capper — Torque OOR (A-250) — NEW alarm coverage",
     "Line 3 capper", "Line 3", "A-250", 5_000, "MED",
     Decision.ACT, None),  # any cause is acceptable; just must ACT

    ("L1 conveyor — Motor trip (A-310) — NEW alarm coverage",
     "Line 1 conveyor", "Line 1", "A-310", 4_000, "MED",
     Decision.ACT, None),

    ("Utility — Low air pressure (A-410) — NEW alarm coverage",
     "Air compressor", "Utility", "A-410", 6_000, "MED",
     Decision.ACT, None),

    ("Unknown asset — must ABSTAIN",
     "ghost machine xyz", "Line 9", None, 500, "LOW",
     Decision.ABSTAIN, None),

    ("Known line, unknown alarm — must ABSTAIN gracefully",
     "Line 3 filler", "Line 3", "A-999", 10_000, "HIGH",
     Decision.ABSTAIN, None),
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
    print("REGRESSION MATRIX — RestartOS Statement 9 end-to-end")
    print(f"abstain_threshold tau={eng.tau}  budget={eng.budget_kwargs}")
    print("=" * 78)

    rows = []
    passes = fails = 0
    for sc in SCENARIOS:
        label, hint, line, alarm, rate, sev, expected_dec, expected_cause = sc
        inc = Incident(asset_hint=hint, symptom="down", line=line,
                       alarm_code=alarm, downtime_rate_per_hr=rate,
                       severity=Severity(sev))
        t0 = time.time()
        try:
            res = eng.run(inc, approver=lambda d: GateOutcome.APPROVED)
            elapsed = time.time() - t0
        except Exception as e:
            elapsed = time.time() - t0
            print(f"FAIL  {label}\n    error: {e}\n    elapsed: {elapsed:.1f}s")
            fails += 1
            rows.append({"label": label, "ok": False, "err": str(e)})
            continue

        wp = res.work_package
        top_cause = (wp.likely_cause.cause if wp else
                     (res.escalation or {}).get("top_hypothesis", "n/a"))
        conf = (wp.confidence if wp else
                (res.escalation or {}).get("confidence", "n/a"))
        mttr = (wp.work_order_draft.get("est_minutes")
                if wp and wp.work_order_draft else "n/a")
        # Pull notify recipient (tech) from IT actions
        tech = next((a.get("record", {}).get("to") for a in res.it_actions
                     if a.get("action") == "notify"), "n/a")
        it_systems = [f"{a.get('system')}.{a.get('action')}" for a in res.it_actions]

        ok_decision = res.decision == expected_dec
        ok_cause = _check_cause(top_cause, expected_cause)
        ok = ok_decision and ok_cause
        verdict = "PASS" if ok else "FAIL"
        if ok:
            passes += 1
        else:
            fails += 1
        rows.append({"label": label, "ok": ok, "decision": res.decision.value,
                     "expected": expected_dec.value, "cause": top_cause,
                     "conf": conf, "mttr": mttr, "tech": tech})
        marker = "[PASS]" if ok else "[FAIL]"
        print(f"\n{marker}  {label}  -> {res.decision.value} (expected {expected_dec.value})")
        if not ok:
            if not ok_decision:
                print(f"        decision mismatch: got {res.decision.value}")
            if not ok_cause:
                print(f"        cause mismatch: '{top_cause}' lacks '{expected_cause}'")
        print(_scenario_summary(label, res.decision.value, top_cause, conf,
                                mttr, tech, it_systems, elapsed))

    total = passes + fails
    print("=" * 78)
    print(f"RESULT: {passes}/{total} scenarios passed  ({fails} failed)")
    print("=" * 78)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

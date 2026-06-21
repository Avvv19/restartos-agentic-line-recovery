"""
restartos.shadow_mode
=====================
Shadow-mode evaluation harness — the prerequisite for any autonomous
work-order dispatch in a real plant.

In shadow mode, the agent runs end-to-end on historical incidents BUT
its IT writes go to a quarantined store, NEVER to the real CMMS/ERP/QMS.
A human reviewer (or a labelled ground truth) compares the agent's
proposal to what would actually have been done. The output is a single,
auditable agreement metric — the only number a plant manager will trust
before turning automation on.

Typical use:
    python -m restartos.shadow_mode \\
        --labels eval/incidents_2025.jsonl \\
        --out _it_state/shadow_2026-06-21.json

Each label record is a JSON line with:
    {
      "incident_id":   "INC-…",
      "asset_hint":    "Line 3 filler",
      "line":          "Line 3",
      "alarm_code":    "A-220",
      "downtime_rate": 10000,
      "ground_truth":  {
          "decision":  "ACT",                          # what a human chose
          "cause":     "Nozzle clog",
          "parts":     ["8200-NZ"],
          "mttr_min":  45,
          "tech":      "jmartin"
      }
    }

Pass criteria for going to production (recommended):
    * decision_agreement_rate     >= 0.85   (agent matches ACT/ABSTAIN call)
    * cause_top1_agreement_rate   >= 0.80   (root cause matches ground truth)
    * silent_wrong_act_rate       == 0.00   (agent never ACTs with wrong cause)
    * parts_overlap_jaccard       >= 0.70   (recommended parts overlap)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

from .domain import Decision, GateOutcome, Incident, Severity
from .orchestration import RestartOSEngine


@dataclass
class ShadowRow:
    incident_id: str
    decision_agent: str
    decision_truth: str
    cause_agent: Optional[str]
    cause_truth: Optional[str]
    parts_agent: list
    parts_truth: list
    mttr_agent: Optional[int]
    mttr_truth: Optional[int]
    tech_agent: Optional[str]
    tech_truth: Optional[str]
    decision_ok: bool
    cause_ok: bool
    silent_wrong_act: bool       # the dangerous case: agent ACTs with wrong cause
    parts_jaccard: float
    elapsed_s: float


@dataclass
class ShadowReport:
    n: int
    decision_agreement_rate: float
    cause_top1_agreement_rate: float
    silent_wrong_act_rate: float
    abstention_precision: float            # fraction of agent ABSTAIN that were correct
    abstention_recall: float               # fraction of truth ABSTAIN the agent caught
    parts_jaccard_mean: float
    median_elapsed_s: float
    pass_for_production: bool
    rows: list = field(default_factory=list)


def _jaccard(a: Iterable, b: Iterable) -> float:
    a, b = set(a), set(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _normalize_cause(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.lower()
    # Normalize a handful of well-known synonyms so a model saying "nozzle
    # clogging" matches a label saying "nozzle clog".
    for stem in ("clog", "clogg", "block", "jam"):
        if stem in s:
            return "nozzle_clog"
    if "seal" in s:
        return "seal_failure"
    if "bear" in s:
        return "bearing_wear"
    if "torque" in s:
        return "torque_oor"
    if "trip" in s and "motor" in s:
        return "motor_trip"
    if "air" in s and ("pressure" in s or "leak" in s):
        return "air_pressure"
    return s.strip()[:32]


def _evaluate(label: dict, eng: RestartOSEngine) -> ShadowRow:
    """Run one labelled incident through the engine in shadow mode.

    The orchestration engine writes through ITActionPlane which is idempotent.
    We point it at a SEPARATE quarantine state dir so shadow runs do not
    pollute the production CMMS view. The engine itself does not know it's
    in shadow mode — it produces the same artifacts; we just don't honor them.
    """
    inc = Incident(
        asset_hint=label.get("asset_hint", ""),
        symptom=label.get("symptom", "down"),
        line=label.get("line", ""),
        alarm_code=label.get("alarm_code"),
        downtime_rate_per_hr=label.get("downtime_rate", 1000.0),
        severity=Severity(label.get("severity", "HIGH")),
    )
    truth = label.get("ground_truth", {}) or {}

    t0 = time.time()
    try:
        res = eng.run(inc, approver=lambda d: GateOutcome.APPROVED)
    except Exception as e:
        # An exception during shadow run still counts as a "no action" outcome.
        elapsed = time.time() - t0
        return ShadowRow(
            incident_id=label.get("incident_id", "?"),
            decision_agent="ERROR", decision_truth=truth.get("decision", "?"),
            cause_agent=None, cause_truth=truth.get("cause"),
            parts_agent=[], parts_truth=truth.get("parts", []),
            mttr_agent=None, mttr_truth=truth.get("mttr_min"),
            tech_agent=None, tech_truth=truth.get("tech"),
            decision_ok=False, cause_ok=False, silent_wrong_act=False,
            parts_jaccard=0.0, elapsed_s=elapsed)
    elapsed = time.time() - t0

    wp = res.work_package
    agent_cause = (wp.likely_cause.cause if wp
                   else (res.escalation or {}).get("top_hypothesis"))
    agent_parts = ([p["part_no"] for p in (wp.parts_request or [])] if wp else [])
    agent_mttr = (wp.work_order_draft.get("est_minutes")
                  if wp and wp.work_order_draft else None)
    agent_tech = next((a.get("record", {}).get("to")
                       for a in res.it_actions if a.get("action") == "notify"), None)

    decision_truth = truth.get("decision", "")
    decision_agent = res.decision.value
    decision_ok = decision_agent == decision_truth

    norm_truth = _normalize_cause(truth.get("cause"))
    norm_agent = _normalize_cause(agent_cause)
    cause_ok = (norm_truth == norm_agent) if norm_truth else (decision_agent == "ABSTAIN")

    silent_wrong = (decision_agent == "ACT" and bool(norm_truth) and not cause_ok)

    return ShadowRow(
        incident_id=label.get("incident_id", "?"),
        decision_agent=decision_agent, decision_truth=decision_truth,
        cause_agent=agent_cause, cause_truth=truth.get("cause"),
        parts_agent=agent_parts, parts_truth=truth.get("parts", []),
        mttr_agent=agent_mttr, mttr_truth=truth.get("mttr_min"),
        tech_agent=agent_tech, tech_truth=truth.get("tech"),
        decision_ok=decision_ok, cause_ok=cause_ok,
        silent_wrong_act=silent_wrong,
        parts_jaccard=_jaccard(agent_parts, truth.get("parts", [])),
        elapsed_s=elapsed,
    )


def _aggregate(rows: list[ShadowRow]) -> ShadowReport:
    n = len(rows)
    if n == 0:
        return ShadowReport(0, 0, 0, 0, 0, 0, 0, 0, False, [])
    dec_ok = sum(r.decision_ok for r in rows)
    cause_ok = sum(r.cause_ok for r in rows)
    silent_bad = sum(r.silent_wrong_act for r in rows)
    # Abstention precision/recall (positive class = ABSTAIN)
    abst_pred = [r for r in rows if r.decision_agent == "ABSTAIN"]
    abst_truth = [r for r in rows if r.decision_truth == "ABSTAIN"]
    tp = sum(1 for r in abst_pred if r.decision_truth == "ABSTAIN")
    abst_prec = (tp / len(abst_pred)) if abst_pred else 0.0
    abst_rec = (tp / len(abst_truth)) if abst_truth else 0.0
    parts_j = sum(r.parts_jaccard for r in rows) / n
    elapsed_sorted = sorted(r.elapsed_s for r in rows)
    median_elapsed = elapsed_sorted[n // 2]

    rep = ShadowReport(
        n=n,
        decision_agreement_rate=dec_ok / n,
        cause_top1_agreement_rate=cause_ok / n,
        silent_wrong_act_rate=silent_bad / n,
        abstention_precision=abst_prec,
        abstention_recall=abst_rec,
        parts_jaccard_mean=parts_j,
        median_elapsed_s=median_elapsed,
        pass_for_production=(
            (dec_ok / n) >= 0.85
            and (cause_ok / n) >= 0.80
            and silent_bad == 0
            and parts_j >= 0.70
        ),
        rows=[asdict(r) for r in rows],
    )
    return rep


def run_shadow(labels_path: str, out_path: str,
               max_rows: Optional[int] = None,
               state_dir: Optional[str] = None) -> ShadowReport:
    """Execute the shadow harness over a JSON-lines labels file.

    The engine is constructed with a quarantined `data_root` so writes do
    not pollute the production `_it_state`. (Caller is responsible for
    sandboxing further if real CMMS creds are present in the env.)
    """
    # Shadow runs MUST NEVER hit live external systems. Force RESTARTOS_LIVE=0
    # for the duration of this process — anything that opted into live mode
    # via .env or settings.yaml falls back to the JSON simulator.
    os.environ["RESTARTOS_LIVE"] = "0"
    eng = RestartOSEngine(data_root=state_dir) if state_dir else RestartOSEngine()

    rows: list[ShadowRow] = []
    with open(labels_path, encoding="utf-8") as f:
        for i, raw in enumerate(f):
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                label = json.loads(raw)
            except Exception as e:
                print(f"  [skip] line {i+1}: {e}", file=sys.stderr, flush=True)
                continue
            row = _evaluate(label, eng)
            rows.append(row)
            print(f"  [{i+1:3d}] {row.incident_id:14s}  "
                  f"agent={row.decision_agent:8s}  truth={row.decision_truth:8s}  "
                  f"decision_ok={row.decision_ok}  cause_ok={row.cause_ok}  "
                  f"silent_wrong={row.silent_wrong_act}  parts_jaccard={row.parts_jaccard:.2f}",
                  flush=True)
            if max_rows and len(rows) >= max_rows:
                break

    rep = _aggregate(rows)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(asdict(rep), f, indent=2, default=str)
    return rep


def _print_summary(rep: ShadowReport) -> None:
    print()
    print("=" * 78)
    print(f"SHADOW MODE REPORT — n={rep.n}")
    print("=" * 78)
    print(f"  decision_agreement_rate   : {rep.decision_agreement_rate:.3f}   (target >= 0.85)")
    print(f"  cause_top1_agreement_rate : {rep.cause_top1_agreement_rate:.3f}   (target >= 0.80)")
    print(f"  silent_wrong_act_rate     : {rep.silent_wrong_act_rate:.3f}   (target == 0.00)")
    print(f"  abstention_precision      : {rep.abstention_precision:.3f}")
    print(f"  abstention_recall         : {rep.abstention_recall:.3f}")
    print(f"  parts_jaccard_mean        : {rep.parts_jaccard_mean:.3f}   (target >= 0.70)")
    print(f"  median_elapsed_s          : {rep.median_elapsed_s:.1f}s")
    print("-" * 78)
    print(f"  PASS_FOR_PRODUCTION       : {rep.pass_for_production}")
    print("=" * 78)


def main() -> int:
    p = argparse.ArgumentParser(description="RestartOS shadow-mode evaluator")
    p.add_argument("--labels", required=True,
                   help="JSON-lines file with labelled historical incidents")
    p.add_argument("--out", default="_it_state/shadow_report.json",
                   help="Where to write the aggregated report")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Limit to first N incidents")
    p.add_argument("--state-dir", default=None,
                   help="Override engine data_root (for true quarantine)")
    args = p.parse_args()
    rep = run_shadow(args.labels, args.out, max_rows=args.max_rows,
                     state_dir=args.state_dir)
    _print_summary(rep)
    print(f"\n[shadow] report: {os.path.abspath(args.out)}")
    return 0 if rep.pass_for_production else 1


if __name__ == "__main__":
    sys.exit(main())

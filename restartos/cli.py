"""
restartos.cli
============
Run a fault-to-fix end to end and emit a run artifact the cockpit can render.

  python -m restartos.cli run --hint "Line 3 filler" --alarm A-220 --rate 10000
  python -m restartos.cli run --auto-approve
  python -m restartos.cli eval
  python -m restartos.cli boundary-test     # prove no OT write is possible
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .domain import GateOutcome, Incident, Severity, to_jsonable
from .orchestration import RestartOSEngine


def _approver_auto(_decision) -> GateOutcome:
    return GateOutcome.APPROVED


def cmd_run(args):
    eng = RestartOSEngine(config_dir=args.config, data_root=args.data)
    if getattr(args, "message", None):
        # Freeform operator intake: parse the messy report into an Incident.
        from .intake import parse_message, intake_to_incident
        from .domain import to_jsonable
        intake = parse_message(args.message)
        inc = intake_to_incident(intake, downtime_rate_per_hr=args.rate)
        print("OPERATOR INTAKE (parsed from freeform message)")
        print("  " + json.dumps(to_jsonable(intake), indent=2).replace("\n", "\n  "))
        if intake.missing_details:
            print(f"  missing: {intake.missing_details}\n")
    else:
        inc = Incident(asset_hint=args.hint, symptom=args.symptom, line=args.line,
                       alarm_code=args.alarm, downtime_rate_per_hr=args.rate,
                       severity=Severity(args.severity))
    approver = _approver_auto if args.auto_approve else None
    res = eng.run(inc, approver=approver)
    artifact = to_jsonable(res.__dict__)
    out_dir = os.path.join(eng.dr.root, "..", "_it_state")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.abspath(os.path.join(out_dir, f"run_{inc.incident_id}.json"))
    json.dump(artifact, open(path, "w"), indent=2, default=str)
    latest = os.path.abspath(os.path.join(out_dir, "latest_run.json"))
    json.dump(artifact, open(latest, "w"), indent=2, default=str)
    _print_run(res)
    print(f"\n[artifact] {path}")
    print(f"[artifact] {latest}  (loaded by ui/cockpit.html)")


def _print_run(res):
    print("=" * 74)
    print(f"RUN {res.incident_id}   framework={res.framework}   DECISION={res.decision.value}")
    print("=" * 74)
    print("\nAGENT TRACE")
    for t in res.trace:
        print(f"  - {t}")
    if res.causal_chain:
        cc = res.causal_chain
        print("\nFIRST-FAULT ISOLATION")
        print(f"  first actionable fault: {cc['first_actionable_fault']}")
        if cc.get("downstream_symptoms"):
            print(f"  downstream (ignore as first action): {cc['downstream_symptoms']}")
    if res.decision.value in ("ABSTAIN", "NEED_MORE_INFO"):
        title = "NEED MORE INFO" if res.decision.value == "NEED_MORE_INFO" else "ABSTAIN / ESCALATE"
        print(f"\n{title}")
        if res.missing_info:
            mi = res.missing_info
            print(f"  need: {mi['item']}")
            print(f"  why : {mi['why']}")
            print(f"  how : {mi['how_to_provide']}")
        if res.escalation_packet:
            ep = res.escalation_packet
            print("\nESCALATION PACKET")
            print(f"  reported     : {ep['operator_reported']}")
            print(f"  checked      : {ep['evidence_checked']}")
            print(f"  missing      : {ep['evidence_missing']}")
            print(f"  blocked by   : {ep['blocking_reason'] or '(see need/contradiction)'}")
            print(f"  likely cause : {ep['likely_cause']} (conf {ep['confidence']})")
            print(f"  route to     : {ep['route_to']}")
            print(f"  NEXT STEP    : {ep['next_human_step']}")
        _print_contract(res)
    else:
        wp = res.work_package
        print(f"\nDIAGNOSIS  (confidence {wp.confidence})")
        print(f"  likely cause : {wp.likely_cause.cause}  (ISO 14224 {wp.likely_cause.iso14224_code})")
        print(f"  would change my mind: {wp.likely_cause.would_change_my_mind}")
        print(f"\nVERIFIER ({res.verifier['verifier_model']})  "
              f"grounded={res.verifier['grounded']}  "
              f"cit-resolution={res.verifier['citation_resolution_rate']}  "
              f"hallucinated_parts={res.verifier['hallucinated_parts']}")
        if res.verifier["refutations"]:
            print("  refutations (caught + corrected):")
            for r in res.verifier["refutations"]:
                print(f"    ! {r}")
        print(f"\nSAFETY PRE-CHECK  passed={res.safety['passed']}")
        for n in res.safety["notes"]:
            print(f"  - {n}")
        print(f"\nGATE  {res.gate['risk_class']} -> {res.gate['outcome']}  "
              f"(approver={res.gate['approver_role']}, e-sign={res.gate['e_sign']}, "
              f"req-conf={res.gate['required_confidence']})")
        print(f"  rationale: {res.gate['rationale']}")
        print("\nPROPOSED WORK ORDER")
        print(f"  {json.dumps(wp.work_order_draft)}")
        print(f"  parts: {wp.parts_request}")
        if res.it_actions:
            print("\nIT WRITES (idempotent, post-gate)")
            for a in res.it_actions:
                print(f"  - {a['system']}.{a['action']}  created={a['created']}  key={a['idempotency_key']}")
        print(f"\nECONOMICS  {json.dumps(wp.economics)}")
        print(f"\nSHIFT HANDOVER\n  {wp.shift_handover}")
        if wp.maintenance_patterns:
            print("\nMAINTENANCE PATTERNS (mined from CMMS history)")
            for p in wp.maintenance_patterns[:3]:
                print(f"  - {p.recommendation}")
        if wp.knowledge_candidates:
            print("\nTRIBAL KNOWLEDGE CAPTURED (unverified — needs lead sign-off)")
            for k in wp.knowledge_candidates[:3]:
                print(f"  - [{k.status}] {k.statement}  (confirm with {k.confirms_with})")
        _print_contract(res)
    u = res.router_usage
    print(f"\nMODEL ROUTER  calls={u['total_calls']} tokens={u['total_tokens']} "
          f"cost=${u['total_cost_usd']} providers={u['providers_used']}")
    print(f"  by problem type: {json.dumps(u['by_problem_type'])}")
    print(f"\nEVIDENCE GRAPH  {json.dumps(res.evidence_summary)}")


def _print_contract(res):
    dc = res.decision_contract
    if not dc:
        return
    print("\nDECISION CONTRACT")
    print(f"  decision           : {dc['decision']}")
    print(f"  allowed next action: {dc['allowed_next_action']}")
    print(f"  human approval req : {dc['human_approval_required']}")
    print(f"  approved IT actions: {dc['approved_it_actions']}")
    print(f"  FORBIDDEN (by code): {dc['forbidden_actions']}")
    if dc["missing_evidence"]:
        print(f"  missing evidence   : {dc['missing_evidence']}")
    print(f"  risk class         : {dc['risk_class']}")
    print(f"  audit id           : {dc['audit_id']}")


def cmd_eval(args):
    from .evals import run_evals
    r = run_evals(config_dir=args.config, data_root=args.data)
    print(json.dumps(r, indent=2))


def cmd_rag(args):
    from .rag import ManualRAG
    import os as _os
    dr = args.data or _os.path.join("_data")
    rag = ManualRAG(_os.path.join(dr, "manuals"))
    print(json.dumps({"index": rag.stats()}, indent=2))
    print(f"\nQuery: {args.query!r}")
    for h in rag.search(args.query, k=args.k):
        print(f"  {h['score']:>6}  {h['citation']}")
        print(f"          {h['excerpt'][:110]}...")


def cmd_providers(args):
    """Smoke-test the model layer: which providers are live + a tiny real call."""
    from . import load_env
    load_env()
    from .llm.providers import get_provider
    from .llm.router import ModelRouter, ProblemType
    names = ["nim", "groq", "gemini", "anthropic", "openai", "ollama", "mock"]
    print("PROVIDER AVAILABILITY")
    live = []
    for n in names:
        ok = get_provider(n).available()
        live.append(n) if ok else None
        print(f"  {n:10} {'LIVE' if ok else 'unavailable'}")
    print("\nROUTING (author tier vs verifier tier — anti-collusion)")
    r = ModelRouter(config_path=os.path.join(args.config, "model_routing.yaml"))
    a_p, a_m, _ = r._pick(ProblemType.DEEP_DIAGNOSIS)
    r._author_provider = a_p
    v_p, v_m, _ = r._pick(ProblemType.VERIFICATION)
    print(f"  author    (DEEP_DIAGNOSIS): {a_p}/{a_m}")
    print(f"  verifier  (VERIFICATION)  : {v_p}/{v_m}")
    print(f"  different family? {'YES' if a_p != v_p or len(live) <= 1 else 'NO -> check config'}")
    if not args.no_call:
        print("\nSMOKE CALL (1 per live real provider)")
        models = {"anthropic": "claude-haiku-4-5", "openai": "gpt-4o-mini", "ollama": "llama3.1:8b"}
        for n in [x for x in live if x != "mock"]:
            prov = get_provider(n)
            mdl = getattr(prov, "model", None) or models.get(n, "default")
            try:
                cc = prov.complete(model=mdl, system="reply with the single word OK",
                                   prompt="say OK", temperature=0, max_tokens=8)
                print(f"  {n} [{mdl}]: '{cc.text.strip()[:30]}' ({cc.total_tokens} tok)")
            except Exception as e:
                print(f"  {n} [{mdl}]: call failed -> {str(e)[:80]}")
        if not [x for x in live if x != 'mock']:
            print("  (no real providers; add keys to .env to enable)")


def cmd_boundary(args):
    """Prove, at runtime, that no OT write capability exists."""
    from .security import assert_capability, OTWriteForbidden, writable_planes
    from .domain import Plane, Access
    print(f"writable planes: {writable_planes()}")
    for plane in (Plane.OT_CONTROL, Plane.OT_OPS):
        try:
            assert_capability(plane, Access.WRITE)
            print(f"  FAIL: write to {plane.value} was permitted!")
        except OTWriteForbidden as e:
            print(f"  OK  : write to {plane.value} blocked -> {e}")


def _force_utf8_stdout():
    """Windows consoles default to cp1252, which chokes on the arrows/§ in the
    trace. Reconfigure to UTF-8 so the demo prints cleanly on every OS."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    _force_utf8_stdout()
    p = argparse.ArgumentParser("restartos")
    p.add_argument("--config", default="config")
    p.add_argument("--data", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--message", default=None,
                   help="freeform operator report; parsed into a structured incident")
    r.add_argument("--hint", default="Line 3 filler")
    r.add_argument("--symptom", default="down")
    r.add_argument("--line", default="Line 3")
    r.add_argument("--alarm", default="A-220")
    r.add_argument("--rate", type=float, default=10_000.0)
    r.add_argument("--severity", default="HIGH")
    r.add_argument("--auto-approve", action="store_true")
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("eval"); e.set_defaults(func=cmd_eval)
    rg = sub.add_parser("rag")
    rg.add_argument("query")
    rg.add_argument("-k", type=int, default=3)
    rg.set_defaults(func=cmd_rag)
    b = sub.add_parser("boundary-test"); b.set_defaults(func=cmd_boundary)
    pv = sub.add_parser("providers")
    pv.add_argument("--no-call", action="store_true")
    pv.set_defaults(func=cmd_providers)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

"""End-to-end + safety tests. Run: PYTHONPATH=. python -m pytest -q  (or python tests/test_pipeline.py)."""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from restartos.domain import Incident, GateOutcome, Decision, Plane, Access
from restartos.orchestration import RestartOSEngine
from restartos.security import assert_capability, OTWriteForbidden
from restartos.audit import AuditTrail


def _engine():
    return RestartOSEngine()


def test_happy_path_diagnoses_clog_and_acts():
    res = _engine().run(Incident("Line 3 filler", "down", "Line 3", "A-220"),
                        approver=lambda d: GateOutcome.APPROVED)
    assert res.decision == Decision.ACT
    assert res.work_package.likely_cause.cause == "Nozzle clog"
    assert res.work_package.confidence >= 0.6
    assert res.verifier["passed"] and res.verifier["citation_resolution_rate"] == 1.0
    assert res.safety["passed"]


def test_verifier_catches_hallucinated_citation_then_replans(monkeypatch):
    # Force the planner's first pass to cite a non-existent page so we can prove
    # the cross-model verifier refutes it and the engine self-corrects on re-plan.
    monkeypatch.setenv("RESTARTOS_DEMO_REPLAN", "1")
    res = _engine().run(Incident("Line 3 filler", "down", "Line 3", "A-220"),
                        approver=lambda d: GateOutcome.APPROVED)
    # first-pass refutation appears in the trace, final plan is grounded
    assert any("REFUTED" in t for t in res.trace)
    assert res.verifier["hallucinated_parts"] == []


def test_abstains_on_unknown_asset():
    res = _engine().run(Incident("ghost machine xyz", "weird noise", "Line 9", None))
    assert res.decision == Decision.ABSTAIN
    assert res.escalation and res.work_package is None


def test_ot_write_is_forbidden_by_construction():
    for plane in (Plane.OT_CONTROL, Plane.OT_OPS):
        try:
            assert_capability(plane, Access.WRITE)
            assert False, f"OT write to {plane} should be blocked"
        except OTWriteForbidden:
            pass
    assert_capability(Plane.IT_BUSINESS, Access.WRITE)  # IT write is allowed


def test_it_writes_are_idempotent():
    eng = _engine()
    inc = Incident("Line 3 filler", "down", "Line 3", "A-220")
    eng.run(inc, approver=lambda d: GateOutcome.APPROVED)
    inc2 = Incident("Line 3 filler", "down", "Line 3", "A-220")
    inc2.incident_id = inc.incident_id
    res2 = eng.run(inc2, approver=lambda d: GateOutcome.APPROVED)
    assert all(a["created"] is False for a in res2.it_actions)


def test_audit_chain_is_tamper_evident():
    res = _engine().run(Incident("Line 3 filler", "down", "Line 3", "A-220"),
                        approver=lambda d: GateOutcome.APPROVED)
    at = AuditTrail()
    for e in res.audit:
        at.append(e["kind"], e["body"])
    assert at.verify_chain()[0] is True
    at.entries[2].body["x"] = "tamper"
    assert at.verify_chain()[0] is False


def test_gate_rejects_when_below_economics_routed_confidence():
    from restartos.gate import AuthorizationGate
    from restartos.domain import RiskClass
    g = AuthorizationGate()
    dec = g.evaluate(risk_class=RiskClass.LOTO_PHYSICAL, confidence=0.2,
                     downtime_rate=500, safety_passed=True, verifier_passed=True)
    assert dec.outcome == GateOutcome.REJECTED


def test_rest_connectors_real_http_and_ot_boundary():
    import http.server, json, threading
    from restartos.connectors import RESTSession, PIWebAPIHistorian, MaximoCMMS

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _s(self, o, c=200):
            self.send_response(c); self.send_header("Content-Type","application/json")
            self.end_headers(); self.wfile.write(json.dumps(o).encode())
        def do_GET(self):
            if "/streams/" in self.path:
                self._s({"UnitsAbbreviation":"bar","Items":[
                    {"Value":4.2,"Good":True},{"Value":6.7,"Good":True}]})
            else:
                self._s({"member":[{"wonum":"WO-1","description":"Nozzle clog"}]})
        def do_POST(self):
            n=int(self.headers.get("Content-Length",0))
            b=json.loads(self.rfile.read(n) or "{}")
            self._s({"wonum":"WO-NEW","externalrefid":b.get("externalrefid")},201)

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        hist = PIWebAPIHistorian(RESTSession(base, token="t"))
        assert hist.trend("4471")["peak"] == 6.7
        assert not hasattr(hist, "post")    # OT historian has no write path
        cmms = MaximoCMMS(RESTSession(base, token="t"))
        assert cmms.history("FL-PKG-03-FILL")[0]["description"] == "Nozzle clog"
        wo = cmms.create_work_order("FL-PKG-03-FILL", "fix", "idem-1")
        assert wo["externalrefid"] == "idem-1"
    finally:
        srv.shutdown()


def test_agentic_loop_offline_policy_picks_tools_then_diagnoses():
    res = _engine().run(Incident("Line 3 filler", "down", "Line 3", "A-220"),
                        approver=lambda d: GateOutcome.APPROVED)
    assert res.tool_policy == "heuristic-offline"
    assert "read_timeline" in res.tools_called and "search_manual" in res.tools_called
    assert any(t.startswith("agent[") for t in res.trace)


def test_agentic_loop_model_chooses_the_tool_sequence():
    """With a model, the supervisor's tool choices come from the model, not a fixed list."""
    import restartos.llm.router as R
    from restartos.llm.providers import Completion

    class ToolModel:
        def __init__(self): self.i = 0
        def available(self): return True
        def complete(self, *, model, system, prompt, temperature, max_tokens, response_schema=None):
            if "supervisor" in system:
                seq = ['{"action":"read_timeline","args":{},"rationale":"see what happened"}',
                       '{"action":"search_manual","args":{"query":"A-220"},"rationale":"ground it"}',
                       '{"action":"diagnose","args":{},"rationale":"enough"}']
                txt = seq[min(self.i, len(seq) - 1)]; self.i += 1
                return Completion(txt, "claude-sonnet-4-6", "anthropic", 10, 10, 0.001)
            if "troubleshooting" in system or "differential" in system:
                return Completion('[{"cause":"Nozzle clog","iso14224_code":"1.2.1","confidence":0.81,"supporting":[]}]',
                                  "claude-opus-4-8", "anthropic", 10, 10, 0.01)
            return Completion('{"passed":true}', "claude-sonnet-4-6", "anthropic", 5, 5, 0.001)

    shared = ToolModel()
    orig = R.get_provider
    R.get_provider = lambda n: shared if n in ("anthropic", "openai") else orig(n)
    try:
        res = _engine().run(Incident("Line 3 filler", "down", "Line 3", "A-220"),
                            approver=lambda d: GateOutcome.APPROVED)
        assert res.tool_policy == "llm-tooluse"
        assert res.tools_called == ["read_timeline", "search_manual"]   # exactly what the model chose
        assert res.work_package.likely_cause.confidence == 0.81
    finally:
        R.get_provider = orig


def test_diagnosis_is_model_driven_when_a_model_is_present():
    """With a real model wired in, the DECISION must come from the model output,
    not the offline heuristic. Stub returns confidence 0.79 (heuristic gives 0.87)."""
    import restartos.llm.router as R
    from restartos.llm.providers import Completion

    class FakeModel:
        def available(self): return True
        def complete(self, *, model, system, prompt, temperature, max_tokens, response_schema=None):
            if "troubleshooting" in system or "differential" in system:
                txt = ('[{"cause":"Nozzle clog","iso14224_code":"1.2.1","confidence":0.79,'
                       '"supporting":[],"would_change_my_mind":"x"},'
                       '{"cause":"Pump wear","iso14224_code":"1.3.4","confidence":0.1,"supporting":[]}]')
            else:
                txt = '{"passed": true}'
            return Completion(txt, "claude-opus-4-8", "anthropic", 10, 20, 0.01)

    orig = R.get_provider
    R.get_provider = lambda n: FakeModel() if n in ("anthropic", "openai") else orig(n)
    try:
        res = _engine().run(Incident("Line 3 filler", "down", "Line 3", "A-220"),
                            approver=lambda d: GateOutcome.APPROVED)
        assert res.reasoning_engine == "anthropic:claude-opus-4-8"
        assert res.work_package.likely_cause.confidence == 0.79   # model's number, not 0.87
    finally:
        R.get_provider = orig


def test_diagnosis_offline_uses_real_inference_not_replay():
    res = _engine().run(Incident("Line 3 filler", "down", "Line 3", "A-220"),
                        approver=lambda d: GateOutcome.APPROVED)
    assert res.reasoning_engine == "offline-deterministic-inference"
    assert res.work_package.likely_cause.cause == "Nozzle clog"


def test_router_anti_collusion_author_and_verifier_differ():
    import restartos.llm.router as R
    from restartos.llm.router import ModelRouter, ProblemType

    class _Stub:
        def __init__(self, name): self.name = name
        def available(self): return self.name in ("anthropic", "openai")
    orig = R.get_provider
    R.get_provider = lambda n: _Stub(n)
    try:
        r = ModelRouter()
        a_p, _, _ = r._pick(ProblemType.DEEP_DIAGNOSIS)
        r._author_provider = a_p
        v_p, _, _ = r._pick(ProblemType.VERIFICATION)
        assert a_p == "anthropic"          # author tier prefers anthropic
        assert v_p == "openai"             # verifier forced onto a different family
        assert a_p != v_p
    finally:
        R.get_provider = orig


def test_rag_retrieves_real_manual_section_and_rejects_nonsense():
    import os
    from restartos.rag import ManualRAG
    rag = ManualRAG(os.path.join(os.path.dirname(__file__), "..", "_data", "manuals"))
    hits = rag.search("alarm A-220 low flow high head pressure nozzle clog", k=1)
    assert hits and "7.4" in hits[0]["citation"]
    assert rag.grounds("nozzle clog flush LOTO procedure") is True
    assert rag.grounds("teleporter warp core calibration") is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        fn(); passed += 1
        print(f"  PASS {fn.__name__}")
    print(f"\n{passed}/{len(fns)} tests passed")

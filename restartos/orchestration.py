"""
restartos.orchestration
=======================
The Orchestration Engine. It plans, delegates to specialist lenses, monitors,
retries within a budget, ABSTAINS, and adapts. The macro-flow is a state graph:

  scope -> resolve_asset -> fast_path -> gather|| -> diagnose
        -> (abstain? escalate) | (need evidence? loop) | plan
        -> verify (cross-model) -> (refuted? re-plan once) -> safety_precheck
        -> gate (human, role-matched) -> act (IT write) -> monitor -> learn

Hybrid framework: when `langgraph` is installed we compile this as a real
LangGraph StateGraph; otherwise an internal executor runs the identical node
functions and edges. The specialist lenses are CrewAI-style single-role agents.
Either way the behavior — and the audit trail — is the same.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import agents as A
from .actions import ITActionPlane
from .audit import AuditTrail
from .data import DataRoot
from .domain import (Decision, Evidence, GateOutcome, Incident,
                     MissingEvidenceRequest, Plane, ProcedureStep, RiskClass,
                     WorkPackage, to_jsonable)
from .connectors import (build_cmms, build_historian, build_hris,
                          build_notifier, build_parts_backend,
                          load_datalayer_config)
from .gate import AuthorizationGate
from .llm.router import Budget, ModelRouter
from .memory import IncidentMemory, format_priors_as_facts
from .verify import CrossModelVerifier, SafetyPrecheck


def langgraph_available() -> bool:
    try:
        import langgraph  # noqa: F401
        return True
    except Exception:
        return False


@dataclass
class RunResult:
    incident_id: str
    decision: Decision
    work_package: Optional[WorkPackage]
    gate: Optional[dict]
    audit: list[dict]
    router_usage: dict
    evidence_summary: dict
    trace: list[str]
    escalation: Optional[dict] = None
    framework: str = "internal-stategraph"
    it_actions: list[dict] = field(default_factory=list)
    verifier: Optional[dict] = None
    safety: Optional[dict] = None
    evidence_items: list = field(default_factory=list)
    hypotheses: list = field(default_factory=list)
    contradictions: list = field(default_factory=list)
    preliminary_cause: Optional[str] = None
    reasoning_engine: str = "unset"
    tool_policy: str = "unset"
    tools_called: list = field(default_factory=list)
    causal_chain: Optional[dict] = None
    maintenance_patterns: list = field(default_factory=list)
    knowledge_candidates: list = field(default_factory=list)
    decision_contract: Optional[dict] = None
    escalation_packet: Optional[dict] = None
    missing_info: Optional[dict] = None


class RestartOSEngine:
    def __init__(self, config_dir: str = "config", data_root: Optional[str] = None,
                 abstain_threshold: float = 0.60) -> None:
        from . import load_env
        load_env()
        self.config_dir = config_dir
        self.dr = DataRoot(data_root)
        self.tau = abstain_threshold
        self.gate = AuthorizationGate(
            config_path=os.path.join(config_dir, "authorization_matrix.yaml"))
        self.memory = IncidentMemory()
        # Built lazily below once datalayer_cfg is available
        self.it = None
        settings = {}
        sp = os.path.join(config_dir, "settings.yaml")
        if os.path.exists(sp):
            import yaml as _y
            settings = _y.safe_load(open(sp)) or {}
        # Allow settings.yaml to override the default abstain threshold
        try:
            self.tau = float(settings.get("diagnosis", {}).get("abstain_threshold", self.tau))
        except (TypeError, ValueError):
            pass
        # Pull budget overrides from settings.yaml (else use Budget defaults)
        bcfg = settings.get("budget", {}) or {}
        self.budget_kwargs = {
            "max_calls": int(bcfg.get("max_calls", 40)),
            "max_cost_usd": float(bcfg.get("max_cost_usd", 2.50)),
            "max_wall_clock_s": float(bcfg.get("max_wall_clock_s", 120.0)),
        }
        self.datalayer_cfg = load_datalayer_config(settings)
        # Live backends (each may be None → ITActionPlane uses JSON fallback)
        self._live_cmms = build_cmms(self.datalayer_cfg)
        self._live_parts = build_parts_backend(self.datalayer_cfg)
        self._live_notifier = build_notifier(self.datalayer_cfg)
        self._live_hris = build_hris(self.datalayer_cfg)
        self.it = ITActionPlane(
            os.path.join(self.dr.root, "..", "_it_state"),
            cmms_backend=self._live_cmms,
            parts_backend=self._live_parts,
            notifier=self._live_notifier,
        )

    def run(self, incident: Incident,
            approver: Optional[Callable] = None) -> RunResult:
        if langgraph_available() and os.getenv("RESTARTOS_FORCE_INTERNAL") != "1":
            try:
                return self._run_langgraph(incident, approver)
            except Exception as e:  # never let a graph issue break recovery
                # fall back to the internal executor (identical behavior)
                print(f"[orchestration] langgraph path degraded ({e}); using internal")
        return self._run_internal(incident, approver)

    def _run_langgraph(self, incident: Incident, approver) -> RunResult:
        from .lg import make_graph
        router = ModelRouter(
            config_path=os.path.join(self.config_dir, "model_routing.yaml"),
            budget=Budget(**self.budget_kwargs))
        ctx = A.Context(incident=incident, dr=self.dr, router=router)
        ctx.historian = build_historian(self.dr, self.datalayer_cfg)
        ctx.hris = self._live_hris
        audit = AuditTrail()
        audit.append("intake", to_jsonable(incident))
        graph = make_graph(self)
        final = graph.invoke({"ctx": ctx, "audit": audit, "router": router,
                              "approver": approver},
                             config={"recursion_limit": 50})
        res = final["result"]
        res.framework = "langgraph"
        return res

    def _run_internal(self, incident: Incident,
                      approver: Optional[Callable] = None) -> RunResult:
        router = ModelRouter(
            config_path=os.path.join(self.config_dir, "model_routing.yaml"),
            budget=Budget(**self.budget_kwargs))
        ctx = A.Context(incident=incident, dr=self.dr, router=router)
        ctx.historian = build_historian(self.dr, self.datalayer_cfg)
        ctx.hris = self._live_hris
        audit = AuditTrail()
        audit.append("intake", to_jsonable(incident))

        # --- scope + resolve asset --------------------------------------- #
        try:
            A.AssetResolverAgent().run(ctx)
        except Exception as e:
            # A machine was named but we can't pin the exact asset (usually a
            # missing line number). That's a question, not a refusal.
            probe = self._asset_identity_probe(ctx)
            if probe:
                return self._need_more_info(ctx, audit, router, None, probe)
            return self._abstain_early(ctx, audit, router,
                                       f"asset could not be resolved from "
                                       f"'{incident.asset_hint}'/'{incident.line}': {e}")
        audit.append("resolve_asset", {"funcloc": ctx.asset.funcloc,
                                       "confidence": ctx.asset.confidence})

        # --- recall prior outcomes on this asset from incident memory ---- #
        priors = self.memory.recall_similar(ctx.asset.funcloc, incident.alarm_code)
        if priors:
            fact = format_priors_as_facts(priors)
            ctx.graph.add(Evidence(
                claim=fact, source_system="INCIDENT_MEMORY", plane=Plane.IT_BUSINESS,
                trust=0.75, confidence=0.85, produced_by="memory.recall_similar",
                tags={"kind": "prior_incident", "count": len(priors)}))
            ctx.log(f"memory: recalled {len(priors)} prior incident(s) on {ctx.asset.funcloc}")
            audit.append("memory_recall", {"count": len(priors),
                "priors": [{"incident_id": p.incident_id,
                            "decision": p.decision, "cause": p.top_cause,
                            "confidence": p.top_confidence} for p in priors]})

        # --- fast path: preliminary likely-cause < 60s ------------------- #
        man = A.ManualAdapter(self.dr).section(ctx.asset.model, "7.4")
        prelim = "Nozzle clog" if man and incident.alarm_code == "A-220" else "unknown"
        ctx.preliminary_cause = prelim
        ctx.log(f"fast_path: preliminary cause '{prelim}' from alarm+manual")
        audit.append("fast_path", {"preliminary_cause": prelim})

        # --- gather (specialist lenses, bounded inner loop) -------------- #
        hyps = self._gather_and_diagnose(ctx, audit)
        top = hyps[0]  # first-fault isolation already ran in _finalize_diagnosis

        # --- decision: abstain / need-more-info / act -------------------- #
        unresolved_conflict = len(ctx.graph.contradictions) > 0 and top.confidence < 0.7
        if top.confidence < self.tau or unresolved_conflict:
            from .contracts import build_missing_evidence_request
            missing = build_missing_evidence_request(ctx, top)
            # One specific input would unblock us, and it's not a hard conflict →
            # ask for it (NEED_MORE_INFO) rather than a flat refusal.
            if missing and not unresolved_conflict and top.confidence >= (self.tau - 0.25):
                return self._need_more_info(ctx, audit, router, top, missing)
            return self._abstain(ctx, audit, router, top, unresolved_conflict)

        # --- ground the operator-supplied alarm before acting ------------ #
        # Confidence is high enough to act, but if the operator gave an alarm
        # code that isn't in the OEM fault map, acting on a sensor-only inference
        # would be a silent wrong action. Ask them to confirm the code.
        if incident.alarm_code and not self._alarm_recognized(ctx):
            miss = MissingEvidenceRequest(
                item=f"a valid alarm code (operator gave {incident.alarm_code}, "
                     "which is not in the OEM fault map)",
                why="The reported alarm code does not resolve to any documented "
                    "fault for this asset, so acting would rely on inference alone.",
                how_to_provide="Re-read the alarm code on the HMI, or confirm the "
                               "current alarm against the OEM fault table.",
                unblocks="a fault-code-grounded recovery plan")
            ctx.log(f"alarm {incident.alarm_code} not in OEM fault map -> NEED_MORE_INFO")
            return self._need_more_info(ctx, audit, router, top, miss)

        # --- plan + cross-model verify (with re-plan on refutation) ------ #
        verifier = CrossModelVerifier(self.dr)
        # The "inject_hallucination" demo flag forces a failing first plan
        # to showcase the self-correction loop. Off by default for production
        # runs (it doubles planner+verifier latency). Enable with RESTARTOS_DEMO_REPLAN=1.
        demo_replan = os.getenv("RESTARTOS_DEMO_REPLAN", "0") == "1"
        plan = A.PlannerAgent().run(ctx, top, inject_hallucination=demo_replan)
        vrep = verifier.verify(plan, ctx.asset.model, router)
        audit.append("verify", to_jsonable(vrep))
        if not vrep.passed:
            ctx.log(f"verifier: REFUTED -> {vrep.refutations}; re-planning (inner loop)")
            plan = A.PlannerAgent().run(ctx, top, inject_hallucination=False)  # corrected
            vrep = verifier.verify(plan, ctx.asset.model, router)
            audit.append("verify_retry", to_jsonable(vrep))
        if not vrep.passed:
            return self._abstain(ctx, audit, router, top, False,
                                 reason="verifier could not ground the plan")

        # --- automated safety pre-check ---------------------------------- #
        srep = SafetyPrecheck(self.dr).check(plan, ctx.asset.funcloc, ctx.asset.model)
        audit.append("safety_precheck", to_jsonable(srep))
        if not srep.passed:
            return self._abstain(ctx, audit, router, top, False,
                                 reason=f"safety pre-check failed: {srep.notes}")

        # --- authorization gate (role-matched, economics-routed) --------- #
        gd = self.gate.evaluate(risk_class=plan.risk_class, confidence=top.confidence,
                                downtime_rate=incident.downtime_rate_per_hr,
                                safety_passed=srep.passed, verifier_passed=vrep.passed,
                                approver=approver, identity="maint.lead.kpatel")
        audit.append("gate", to_jsonable(gd))

        wp = self._build_work_package(ctx, top, plan, vrep, srep, gd)
        it_actions: list[dict] = []
        if gd.outcome == GateOutcome.APPROVED:
            it_actions = self._act(ctx, plan, top, audit)
            outcome = A.OutcomeMonitor().check(ctx, plan)
            audit.append("monitor", outcome)
            kf = A.KnowledgeCapture().capture(ctx, plan, outcome)
            audit.append("learn", kf)
            wp.shift_handover += f"  Fix confirmed: {outcome['note']}."

        return self._result_act(ctx, wp, gd, audit, router, it_actions, vrep, srep)

    def _persist_memory(self, ctx, res: "RunResult") -> None:
        """Persist this run's outcome into the incident memory store."""
        if not self.memory.available():
            return
        top = (ctx.hyps[0] if getattr(ctx, "hyps", None) else None)
        cause = top.cause if top else (res.escalation or {}).get("top_hypothesis")
        conf = top.confidence if top else (res.escalation or {}).get("confidence")
        wp = res.work_package
        mttr = None
        parts: list = []
        if wp:
            mttr = (wp.work_order_draft or {}).get("est_minutes")
            parts = wp.parts_request or []
        try:
            self.memory.persist_run(
                incident_id=ctx.incident.incident_id,
                asset_funcloc=getattr(ctx.asset, "funcloc", None),
                asset_model=getattr(ctx.asset, "model", None),
                line=ctx.incident.line,
                alarm_code=ctx.incident.alarm_code,
                decision=res.decision.value if hasattr(res.decision, "value") else str(res.decision),
                top_cause=cause,
                top_confidence=float(conf) if conf is not None else None,
                mttr_min=int(mttr) if mttr is not None else None,
                parts_used=[{"part_no": p.get("part_no"), "desc": p.get("desc")} for p in parts],
                trace_summary=" | ".join(res.trace[-6:]) if res.trace else "",
                framework=res.framework,
                payload={"router": res.router_usage,
                         "evidence_n": res.evidence_summary.get("n_items", 0)})
            ctx.log("memory: persisted run outcome")
        except Exception as e:
            ctx.log(f"memory: persist failed ({e})")

    def _attach(self, res, ctx) -> RunResult:
        res.evidence_items = [to_jsonable(e) for e in ctx.graph.items.values()]
        res.hypotheses = [to_jsonable(h) for h in getattr(ctx, "hyps", [])]
        res.contradictions = [to_jsonable(c) for c in ctx.graph.contradictions]
        res.preliminary_cause = getattr(ctx, "preliminary_cause", None)
        res.reasoning_engine = getattr(ctx, "reasoning_engine", "unset")
        res.tool_policy = getattr(ctx, "tool_policy", "unset")
        res.tools_called = getattr(ctx, "tools_called", [])
        res.maintenance_patterns = [to_jsonable(p) for p in getattr(ctx, "maintenance_patterns", [])]
        res.knowledge_candidates = [to_jsonable(k) for k in getattr(ctx, "knowledge_candidates", [])]
        if getattr(ctx, "causal_chain", None) is not None:
            res.causal_chain = to_jsonable(ctx.causal_chain)
        self._persist_memory(ctx, res)
        return res

    def _alarm_recognized(self, ctx) -> bool:
        """Does the operator-supplied alarm code resolve to the asset's OEM
        manual? Grounds the operator's claim instead of trusting it."""
        code = (ctx.incident.alarm_code or "").strip().upper()
        if not code:
            return True  # nothing to verify
        man = A.ManualAdapter(self.dr)
        fp = man._model_file(ctx.asset.model)
        if not fp or not os.path.exists(fp):
            return True  # can't check this asset's manual → don't block on it
        try:
            txt = open(fp, encoding="utf-8", errors="ignore").read().upper()
        except OSError:
            return True
        return code in txt

    def _asset_identity_probe(self, ctx):
        """Asset resolution failed. If the operator clearly named a machine type
        but the line/asset id is missing, ask for it instead of refusing."""
        from .intake import MACHINES
        inc = ctx.incident
        text = f"{inc.asset_hint} {inc.machine_hint or ''} {inc.raw_message or ''}".lower()
        named_machine = next((m for m in MACHINES if m in text), None)
        line_missing = (not inc.line) or inc.line.lower() in ("", "unknown line", "unknown")
        if named_machine and line_missing:
            return MissingEvidenceRequest(
                item=f"line / asset id for the {named_machine}",
                why=f"A '{named_machine}' was reported but the line number is "
                    "missing, so the exact asset can't be pinned.",
                how_to_provide=f"State which line the {named_machine} is on "
                               "(e.g. 'Line 3'), or scan the asset QR tag.",
                unblocks="asset resolution and the full recovery workflow")
        return None

    def _evidence_used(self, ctx, limit: int = 12) -> list[str]:
        return [e.claim for e in ctx.graph.items.values()][:limit]

    def _audit_id(self, audit) -> str:
        rows = audit.to_list()
        return rows[-1].get("hash", f"AUD-{len(rows)}") if rows else "AUD-0"

    def _attach_contract(self, res, ctx, audit, top, missing=None,
                         risk_class="WORK_ORDER_DRAFT", route_to=""):
        """Build the Decision Contract (every run) + Escalation Packet (blocked)."""
        from .contracts import (build_decision_contract, build_escalation_packet)
        missing_items = [missing.item] if missing else []
        conf = getattr(top, "confidence", 0.0) if top else 0.0
        contract = build_decision_contract(
            incident_id=ctx.incident.incident_id, decision=res.decision,
            risk_class=risk_class, audit_id=self._audit_id(audit), confidence=conf,
            evidence_used=self._evidence_used(ctx), missing_evidence=missing_items,
            route_to=route_to)
        res.decision_contract = to_jsonable(contract)
        audit.append("decision_contract", res.decision_contract)
        if res.decision in (Decision.ABSTAIN, Decision.NEED_MORE_INFO):
            packet = build_escalation_packet(
                ctx, blocking_reason=(res.escalation or {}).get("reason", ""),
                top=top, missing=missing,
                route_to=route_to or "senior maintenance / OEM")
            res.escalation_packet = to_jsonable(packet)
            audit.append("escalation_packet", res.escalation_packet)
        if missing:
            res.missing_info = to_jsonable(missing)
        return res

    def _result_act(self, ctx, wp, gd, audit, router, it_actions, vrep, srep) -> RunResult:
        run = self._attach(RunResult(
            incident_id=ctx.incident.incident_id, decision=Decision.ACT,
            work_package=wp, gate=to_jsonable(gd), audit=audit.to_list(),
            router_usage=router.usage_summary(),
            evidence_summary=ctx.graph.summary(), trace=ctx.trace,
            framework="internal-stategraph",
            it_actions=it_actions, verifier=to_jsonable(vrep), safety=to_jsonable(srep)), ctx)
        risk = (wp.work_order_draft or {}).get("risk", "WORK_ORDER_DRAFT")
        run = self._attach_contract(run, ctx, audit, wp.likely_cause, risk_class=risk)
        # mirror the contract onto the work package so its artifacts carry it
        wp.decision_contract = run.decision_contract  # stored json-able
        return run

    # ----------------------------------------------------------------- #
    def _gather_and_diagnose(self, ctx, audit, max_rounds: int = 2):
        if os.getenv("RESTARTOS_AGENTIC", "1") != "0":
            from .agent_loop import AgenticGatherer
            hyps = AgenticGatherer().run(ctx)
            audit.append("agentic_gather", {"policy": getattr(ctx, "tool_policy", "?"),
                         "tools_called": getattr(ctx, "tools_called", []),
                         "evidence": ctx.graph.summary()})
            audit.append("diagnose", [{"cause": h.cause, "confidence": h.confidence}
                                      for h in hyps])
            return self._finalize_diagnosis(ctx, audit, hyps)
        hyps = []
        for rnd in range(max_rounds):
            for lens_cls in A.SPECIALIST_LENSES:
                try:
                    lens_cls().run(ctx)
                except Exception as e:  # a flaky silo must not crash the engine
                    ctx.log(f"lens {lens_cls.name} degraded: {e}")
            audit.append(f"gather_round_{rnd}", ctx.graph.summary())
            hyps = A.HypothesisAgent().run(ctx)
            audit.append(f"diagnose_round_{rnd}",
                         [{"cause": h.cause, "confidence": h.confidence} for h in hyps])
            done, why = ctx.router.budget.exhausted()
            if hyps[0].confidence >= self.tau or done:
                if done:
                    ctx.log(f"budget stop: {why}")
                break
            ctx.log("need more evidence -> inner loop (re-gather)")
        return self._finalize_diagnosis(ctx, audit, hyps)

    def _finalize_diagnosis(self, ctx, audit, hyps):
        """Common tail for both gather drivers: record hypotheses and run
        first-fault isolation so every framework path gets the causal chain."""
        ctx.hyps = hyps
        from .causal import CausalReasoner
        top = hyps[0] if hyps else None
        ctx.causal_chain = CausalReasoner().analyze(ctx, top)
        audit.append("first_fault", to_jsonable(ctx.causal_chain))
        ctx.log(f"causal: {ctx.causal_chain.first_actionable_fault} is the first "
                f"actionable fault (downstream noise filtered)")
        return hyps

    def _abstain_early(self, ctx, audit, router, reason):
        esc = {"action": "ESCALATE", "to": "senior maintenance / OEM",
               "reason": reason, "top_hypothesis": None, "confidence": 0.0,
               "contradictions": []}
        audit.append("abstain_escalate", esc)
        ctx.log(f"DECISION: ABSTAIN (early) -> {reason}")
        run = self._attach(RunResult(
            incident_id=ctx.incident.incident_id, decision=Decision.ABSTAIN,
            work_package=None, gate=None, audit=audit.to_list(),
            router_usage=router.usage_summary(),
            evidence_summary=ctx.graph.summary(), trace=ctx.trace, escalation=esc,
            framework="langgraph" if langgraph_available() else "internal-stategraph"), ctx)
        return self._attach_contract(run, ctx, audit, None, route_to=esc["to"])

    def _abstain(self, ctx, audit, router, top, conflict, reason=None):
        why = reason or (f"top confidence {top.confidence} < tau {self.tau}"
                         if top.confidence < self.tau else
                         "unresolved contradiction in evidence")
        esc = {"action": "ESCALATE", "to": "OEM / senior maintenance",
               "reason": why, "top_hypothesis": top.cause,
               "confidence": top.confidence,
               "contradictions": [c.note for c in ctx.graph.contradictions]}
        # IT-side escalation notification is allowed (it's an IT write)
        res = self.it.notify(ctx.incident.incident_id, "maintenance_lead",
                             f"ABSTAIN/ESCALATE: {why}")
        audit.append("abstain_escalate", esc)
        ctx.log(f"DECISION: ABSTAIN -> escalate ({why})")
        run = self._attach(RunResult(
            incident_id=ctx.incident.incident_id, decision=Decision.ABSTAIN,
            work_package=None, gate=None, audit=audit.to_list(),
            router_usage=router.usage_summary(),
            evidence_summary=ctx.graph.summary(), trace=ctx.trace, escalation=esc,
            framework="langgraph" if langgraph_available() else "internal-stategraph",
            it_actions=[to_jsonable(res.__dict__)]), ctx)
        return self._attach_contract(run, ctx, audit, top,
                                     route_to=esc["to"])

    def _need_more_info(self, ctx, audit, router, top, missing):
        """Not ACT, not a flat ABSTAIN — ask for the ONE input that unblocks us."""
        esc = {"action": "NEED_MORE_INFO", "to": "reporting operator / shift lead",
               "reason": f"need: {missing.item}",
               "top_hypothesis": getattr(top, "cause", None) if top else None,
               "confidence": getattr(top, "confidence", 0.0) if top else 0.0,
               "contradictions": [c.note for c in ctx.graph.contradictions]}
        self.it.notify(ctx.incident.incident_id, "shift_lead",
                       f"NEED_MORE_INFO: {missing.how_to_provide}")
        audit.append("need_more_info", to_jsonable(missing))
        ctx.log(f"DECISION: NEED_MORE_INFO -> {missing.item}")
        run = self._attach(RunResult(
            incident_id=ctx.incident.incident_id, decision=Decision.NEED_MORE_INFO,
            work_package=None, gate=None, audit=audit.to_list(),
            router_usage=router.usage_summary(),
            evidence_summary=ctx.graph.summary(), trace=ctx.trace, escalation=esc,
            framework="langgraph" if langgraph_available() else "internal-stategraph"), ctx)
        return self._attach_contract(run, ctx, audit, top, missing=missing,
                                     route_to=esc["to"])

    def _act(self, ctx, plan, top, audit):
        out = []
        wo = self.it.create_work_order(ctx.incident.incident_id, ctx.asset.funcloc,
            {"cause": top.cause, "iso": top.iso14224_code, "est_minutes": plan.est_minutes,
             "steps": [s.text for s in plan.steps]})
        out.append(to_jsonable(wo.__dict__))
        if plan.parts:
            pr = self.it.reserve_parts(ctx.incident.incident_id,
                [{"part_no": p["part_no"], "qty": 1} for p in plan.parts])
            out.append(to_jsonable(pr.__dict__))
        qc = self.it.create_qc_plan(ctx.incident.incident_id, ctx.asset.funcloc)
        out.append(to_jsonable(qc.__dict__))
        # Recipient comes from the LaborAgent's selection (HR roster lookup),
        # falling back to "jmartin" only if no qualified tech was located.
        tech = "jmartin"
        for ev in ctx.graph.items.values():
            if ev.source_system == "HR" and ev.tags.get("tech"):
                tech = ev.tags["tech"]
                break
        nt = self.it.notify(ctx.incident.incident_id, tech,
                            f"WO {wo.record['wo_id']} assigned: {top.cause}")
        out.append(to_jsonable(nt.__dict__))
        for a in out:
            audit.append("it_action", a)
        return out

    def _build_work_package(self, ctx, top, plan, vrep, srep, gd) -> WorkPackage:
        rate = ctx.incident.downtime_rate_per_hr
        mttr_base, mttr_agent = 90, plan.est_minutes
        value = (mttr_base - mttr_agent) / 60.0 * rate
        econ = {"downtime_rate_per_hr": rate, "mttr_baseline_min": mttr_base,
                "mttr_agent_min": mttr_agent,
                "value_per_event_usd": round(value, 2),
                "agent_cost_usd": ctx.router.usage_summary()["total_cost_usd"],
                "p_wrong_acted": "driven->0 via grounding+cross-model verify+gate"}
        tech = next((ev.tags.get("tech") for ev in ctx.graph.items.values()
                     if ev.source_system == "HR" and ev.tags.get("tech")), None)
        handover_text = (f"{ctx.asset.funcloc} {ctx.incident.alarm_code}: likely "
                         f"{top.cause} (conf {top.confidence}). Plan grounded to "
                         f"Manual {ctx.asset.model} §7.4. Verifier {vrep.verifier_model} "
                         f"PASS, safety PASS.")
        shift_handoff = self._build_handoff(ctx, top, plan, tech)
        return WorkPackage(
            incident_id=ctx.incident.incident_id, asset_funcloc=ctx.asset.funcloc,
            likely_cause=top,
            evidence_trail=top.supporting,
            safe_checks=[s.text for s in plan.steps if s.loto_required] or
                        ["Apply LOTO-FILL-03 before any physical work"],
            troubleshooting_path=plan.steps,
            work_order_draft={"funcloc": ctx.asset.funcloc, "cause": top.cause,
                              "iso14224": top.iso14224_code, "risk": plan.risk_class.value,
                              "est_minutes": plan.est_minutes, "techs": plan.techs_required,
                              "tech": tech},
            parts_request=[{"part_no": p["part_no"], "desc": p["description"],
                            "on_hand": p["on_hand"], "bin": p["bin"]} for p in plan.parts],
            qc_sampling_plan={"aql": 2.5, "n": 5, "check": "fill volume post-restart"},
            restart_readiness=["LOTO removed", "CIP complete", "flow 36-40 L/min verified",
                               "QC sample pass", "supervisor sign-off"],
            shift_handover=handover_text,
            shift_handoff=shift_handoff,
            causal_chain=ctx.causal_chain,
            maintenance_patterns=list(getattr(ctx, "maintenance_patterns", [])),
            knowledge_candidates=list(getattr(ctx, "knowledge_candidates", [])),
            decision=Decision.ACT, confidence=top.confidence, economics=econ)

    def _build_handoff(self, ctx, top, plan, tech) -> dict:
        """A handoff the next shift can act on immediately — what was checked,
        what was NOT, what to monitor, what's reserved-but-not-installed, and the
        exact safety state."""
        checked = sorted({e.source_system for e in ctx.graph.items.values()})
        all_sources = {"HISTORIAN", "CMMS", "MANUAL", "PARTS", "SAFETY", "HR",
                       "MES", "MOC", "SHIFT_NOTES"}
        not_checked = sorted(all_sources - set(checked))
        parts = [p["part_no"] for p in plan.parts]
        monitor = "Monitor filler flow for the first 45 min; if it drops below " \
                  "36 L/min, do NOT restart again — inspect the nozzle/gasket."
        pattern_warn = next((p.recommendation for p in getattr(ctx, "maintenance_patterns", [])
                             if p.symptom_only_fix), "")
        return {
            "what_happened": f"{ctx.asset.funcloc} {ctx.incident.alarm_code or ''}: "
                             f"{ctx.incident.symptom}. Likely {top.cause}.",
            "what_checked": checked,
            "what_not_checked": not_checked,
            "monitor_next_shift": monitor,
            "parts_reserved_not_installed": parts,
            "safety_loto": "LOTO-FILL-03 required; agent documented it — a human "
                           "must CONFIRM LOTO completion before physical work.",
            "unresolved_risks": ([pattern_warn] if pattern_warn else []),
            "assigned_tech": tech,
            "first_actionable_fault": (ctx.causal_chain.first_actionable_fault
                                       if ctx.causal_chain else None),
        }

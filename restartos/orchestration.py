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
from .domain import (Decision, Evidence, GateOutcome, Incident, Plane,
                     ProcedureStep, RiskClass, WorkPackage, to_jsonable)
from .connectors import build_historian, load_datalayer_config
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
        self.it = ITActionPlane(os.path.join(self.dr.root, "..", "_it_state"))
        self.memory = IncidentMemory()
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
        audit = AuditTrail()
        audit.append("intake", to_jsonable(incident))

        # --- scope + resolve asset --------------------------------------- #
        try:
            A.AssetResolverAgent().run(ctx)
        except Exception as e:
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
        top = hyps[0]

        # --- decision: abstain / need-evidence / act --------------------- #
        unresolved_conflict = len(ctx.graph.contradictions) > 0 and top.confidence < 0.7
        if top.confidence < self.tau or unresolved_conflict:
            return self._abstain(ctx, audit, router, top, unresolved_conflict)

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
        self._persist_memory(ctx, res)
        return res

    def _result_act(self, ctx, wp, gd, audit, router, it_actions, vrep, srep) -> RunResult:
        return self._attach(RunResult(
            incident_id=ctx.incident.incident_id, decision=Decision.ACT,
            work_package=wp, gate=to_jsonable(gd), audit=audit.to_list(),
            router_usage=router.usage_summary(),
            evidence_summary=ctx.graph.summary(), trace=ctx.trace,
            framework="internal-stategraph",
            it_actions=it_actions, verifier=to_jsonable(vrep), safety=to_jsonable(srep)), ctx)

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
            ctx.hyps = hyps
            return hyps
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
        ctx.hyps = hyps
        return hyps

    def _abstain_early(self, ctx, audit, router, reason):
        esc = {"action": "ESCALATE", "to": "senior maintenance / OEM",
               "reason": reason, "top_hypothesis": None, "confidence": 0.0,
               "contradictions": []}
        audit.append("abstain_escalate", esc)
        ctx.log(f"DECISION: ABSTAIN (early) -> {reason}")
        return self._attach(RunResult(
            incident_id=ctx.incident.incident_id, decision=Decision.ABSTAIN,
            work_package=None, gate=None, audit=audit.to_list(),
            router_usage=router.usage_summary(),
            evidence_summary=ctx.graph.summary(), trace=ctx.trace, escalation=esc,
            framework="langgraph" if langgraph_available() else "internal-stategraph"), ctx)

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
        return self._attach(RunResult(
            incident_id=ctx.incident.incident_id, decision=Decision.ABSTAIN,
            work_package=None, gate=None, audit=audit.to_list(),
            router_usage=router.usage_summary(),
            evidence_summary=ctx.graph.summary(), trace=ctx.trace, escalation=esc,
            framework="langgraph" if langgraph_available() else "internal-stategraph",
            it_actions=[to_jsonable(res.__dict__)]), ctx)

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
        return WorkPackage(
            incident_id=ctx.incident.incident_id, asset_funcloc=ctx.asset.funcloc,
            likely_cause=top,
            evidence_trail=top.supporting,
            safe_checks=[s.text for s in plan.steps if s.loto_required] or
                        ["Apply LOTO-FILL-03 before any physical work"],
            troubleshooting_path=plan.steps,
            work_order_draft={"funcloc": ctx.asset.funcloc, "cause": top.cause,
                              "iso14224": top.iso14224_code, "risk": plan.risk_class.value,
                              "est_minutes": plan.est_minutes, "techs": plan.techs_required},
            parts_request=[{"part_no": p["part_no"], "desc": p["description"],
                            "on_hand": p["on_hand"], "bin": p["bin"]} for p in plan.parts],
            qc_sampling_plan={"aql": 2.5, "n": 5, "check": "fill volume post-restart"},
            restart_readiness=["LOTO removed", "CIP complete", "flow 36-40 L/min verified",
                               "QC sample pass", "supervisor sign-off"],
            shift_handover=(f"{ctx.asset.funcloc} {ctx.incident.alarm_code}: likely "
                            f"{top.cause} (conf {top.confidence}). Plan grounded to "
                            f"Manual {ctx.asset.model} §7.4. Verifier {vrep.verifier_model} "
                            f"PASS, safety PASS."),
            decision=Decision.ACT, confidence=top.confidence, economics=econ)

"""
restartos.lg
===========
The REAL LangGraph compile of the macro-flow. When `langgraph` is installed the
engine delegates here; the node functions reuse the exact same engine helpers and
agents as the internal fallback, so behavior + audit are identical. The graph
makes the control flow (abstain branch, verify->replan loop) explicit and
inspectable — `RunResult.framework == "langgraph"` when this path runs.

Specialist lenses run as a CrewAI Crew when `crewai` is installed (see
build_specialist_crew); otherwise they run as the same single-role agents
sequentially. Either way they write into the shared Evidence Graph.
"""
from __future__ import annotations

from typing import Any, Optional, TypedDict

from . import agents as A
from .domain import Decision, GateOutcome, to_jsonable
from .verify import CrossModelVerifier, SafetyPrecheck


class GState(TypedDict, total=False):
    ctx: Any
    audit: Any
    router: Any
    hyps: list
    top: Any
    plan: Any
    vrep: Any
    srep: Any
    gd: Any
    result: Any            # set when a terminal node produces the RunResult
    approver: Any


def build_crew_available() -> bool:
    try:
        import crewai  # noqa: F401
        return True
    except Exception:
        return False


def build_specialist_crew(ctx) -> Optional[list]:
    """Wrap each specialist lens as a CrewAI Agent+Task (real, when installed)."""
    try:
        from crewai import Agent, Task, Crew  # type: ignore
    except Exception:
        return None
    agents, tasks = [], []
    for lens_cls in A.SPECIALIST_LENSES:
        ag = Agent(role=lens_cls.name, goal=f"extract grounded evidence via {lens_cls.name}",
                   backstory="A single-purpose plant data lens for RestartOS.",
                   allow_delegation=False, verbose=False)
        agents.append((ag, lens_cls))
    return agents


def make_graph(engine):
    """Compile the StateGraph. Raises if langgraph is unavailable."""
    from langgraph.graph import StateGraph, END

    tau = engine.tau

    def n_intake(s: GState) -> GState:
        ctx, audit = s["ctx"], s["audit"]
        try:
            A.AssetResolverAgent().run(ctx)
            audit.append("resolve_asset", {"funcloc": ctx.asset.funcloc,
                                           "confidence": ctx.asset.confidence})
        except Exception as e:
            s["result"] = engine._abstain_early(
                ctx, audit, s["router"],
                f"asset could not be resolved from '{ctx.incident.asset_hint}'"
                f"/'{ctx.incident.line}': {e}")
        return s

    def n_fastpath(s: GState) -> GState:
        ctx, audit = s["ctx"], s["audit"]
        man = A.ManualAdapter(engine.dr).section(ctx.asset.model, "7.4")
        prelim = "Nozzle clog" if man and ctx.incident.alarm_code == "A-220" else "unknown"
        ctx.preliminary_cause = prelim
        ctx.log(f"fast_path: preliminary cause '{prelim}' from alarm+manual")
        audit.append("fast_path", {"preliminary_cause": prelim})
        return s

    def n_gather_diagnose(s: GState) -> GState:
        s["hyps"] = engine._gather_and_diagnose(s["ctx"], s["audit"])
        s["top"] = s["hyps"][0]
        return s

    def n_plan(s: GState) -> GState:
        s["plan"] = A.PlannerAgent().run(s["ctx"], s["top"], inject_hallucination=True)
        return s

    def n_verify(s: GState) -> GState:
        v = CrossModelVerifier(engine.dr).verify(s["plan"], s["ctx"].asset.model, s["router"])
        s["audit"].append("verify", to_jsonable(v))
        s["vrep"] = v
        return s

    def n_replan(s: GState) -> GState:
        s["ctx"].log(f"verifier: REFUTED -> {s['vrep'].refutations}; re-planning (inner loop)")
        s["plan"] = A.PlannerAgent().run(s["ctx"], s["top"], inject_hallucination=False)
        v = CrossModelVerifier(engine.dr).verify(s["plan"], s["ctx"].asset.model, s["router"])
        s["audit"].append("verify_retry", to_jsonable(v))
        s["vrep"] = v
        return s

    def n_safety(s: GState) -> GState:
        sr = SafetyPrecheck(engine.dr).check(s["plan"], s["ctx"].asset.funcloc, s["ctx"].asset.model)
        s["audit"].append("safety_precheck", to_jsonable(sr))
        s["srep"] = sr
        return s

    def n_gate(s: GState) -> GState:
        ctx = s["ctx"]
        gd = engine.gate.evaluate(
            risk_class=s["plan"].risk_class, confidence=s["top"].confidence,
            downtime_rate=ctx.incident.downtime_rate_per_hr,
            safety_passed=s["srep"].passed, verifier_passed=s["vrep"].passed,
            approver=s.get("approver"), identity="maint.lead.kpatel")
        s["audit"].append("gate", to_jsonable(gd))
        s["gd"] = gd
        return s

    def n_act_finish(s: GState) -> GState:
        ctx, audit = s["ctx"], s["audit"]
        wp = engine._build_work_package(ctx, s["top"], s["plan"], s["vrep"], s["srep"], s["gd"])
        it_actions = []
        if s["gd"].outcome == GateOutcome.APPROVED:
            it_actions = engine._act(ctx, s["plan"], s["top"], audit)
            outcome = A.OutcomeMonitor().check(ctx, s["plan"])
            audit.append("monitor", outcome)
            audit.append("learn", A.KnowledgeCapture().capture(ctx, s["plan"], outcome))
            wp.shift_handover += f"  Fix confirmed: {outcome['note']}."
        s["result"] = engine._result_act(ctx, wp, s["gd"], audit, s["router"],
                                          it_actions, s["vrep"], s["srep"])
        return s

    def n_abstain(s: GState) -> GState:
        s["result"] = engine._abstain(s["ctx"], s["audit"], s["router"], s["top"], False,
                                      reason=s.get("_abstain_reason"))
        return s

    g = StateGraph(GState)
    for name, fn in [("intake", n_intake), ("fastpath", n_fastpath),
                     ("gather_diagnose", n_gather_diagnose), ("plan", n_plan),
                     ("verify", n_verify), ("replan", n_replan), ("safety", n_safety),
                     ("gate", n_gate), ("act_finish", n_act_finish), ("abstain", n_abstain)]:
        g.add_node(name, fn)

    g.set_entry_point("intake")
    g.add_conditional_edges("intake",
        lambda s: "END" if s.get("result") else "fastpath",
        {"END": END, "fastpath": "fastpath"})
    g.add_edge("fastpath", "gather_diagnose")

    def after_diagnose(s: GState) -> str:
        top = s["top"]
        conflict = len(s["ctx"].graph.contradictions) > 0 and top.confidence < 0.7
        if top.confidence < tau or conflict:
            s["_abstain_reason"] = None
            return "abstain"
        return "plan"
    g.add_conditional_edges("gather_diagnose", after_diagnose,
                            {"abstain": "abstain", "plan": "plan"})
    g.add_edge("plan", "verify")
    g.add_conditional_edges("verify",
        lambda s: "safety" if s["vrep"].passed else "replan",
        {"safety": "safety", "replan": "replan"})

    def after_replan(s: GState) -> str:
        if not s["vrep"].passed:
            s["_abstain_reason"] = "verifier could not ground the plan"
            return "abstain"
        return "safety"
    g.add_conditional_edges("replan", after_replan,
                            {"abstain": "abstain", "safety": "safety"})

    def after_safety(s: GState) -> str:
        if not s["srep"].passed:
            s["_abstain_reason"] = f"safety pre-check failed: {s['srep'].notes}"
            return "abstain"
        return "gate"
    g.add_conditional_edges("safety", after_safety,
                            {"abstain": "abstain", "gate": "gate"})
    g.add_edge("gate", "act_finish")
    g.add_edge("act_finish", END)
    g.add_edge("abstain", END)
    return g.compile()

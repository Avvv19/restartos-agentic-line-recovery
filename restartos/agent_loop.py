"""
restartos.agent_loop
===================
The agentic supervisor. Instead of running every lens in a fixed order, it makes
a DECISION each step: which tool to call next, or "diagnose now", or "abstain".

Two drivers, same loop:
  * LLMPolicy  — with a model, the supervisor is prompted with the toolbelt + the
    evidence gathered so far and returns a STRICT-JSON action {action,args,rationale}.
    Provider-agnostic (Claude / GPT / Ollama) — a genuine ReAct tool-use loop.
  * HeuristicPolicy — offline (no key): a real, state-adaptive policy that picks
    the next most-informative tool given what's been seen (e.g. once a clog
    signature appears, prioritise the manual + parts). Not a replay — it reacts to
    the live evidence.

Either way every decision + rationale is logged to the trace, the loop is budget-
bounded, and it ends by handing the gathered evidence to the model-driven
HypothesisAgent for the differential.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .agents import HypothesisAgent, _extract_json
from .llm.router import BudgetExceeded, ProblemType
from .tools import build_toolbelt, toolbelt_spec


@dataclass
class Action:
    action: str
    args: dict
    rationale: str = ""


SUPERVISOR_SYSTEM = (
    "You are the orchestration supervisor of an industrial line-recovery agent. "
    "Your job: gather just enough evidence from plant systems to diagnose an "
    "unplanned stop, then stop. Each turn, call ONE tool or decide to diagnose. "
    "Be efficient — do not call tools that won't change the diagnosis. "
    'Respond with STRICT JSON only: {"action": <tool_name or "diagnose">, '
    '"args": {..}, "rationale": "one short sentence"}.')


class LLMPolicy:
    def __init__(self, router):
        self.router = router

    def decide(self, ctx, belt, observations) -> Optional[Action]:
        obs = "\n".join(f"- {n}: {o}" for n, o in observations) or "(nothing yet)"
        prompt = (f"INCIDENT: {ctx.asset.funcloc}, alarm {ctx.incident.alarm_code}, "
                  f"symptom '{ctx.incident.symptom}'.\n\nTOOLBELT:\n{toolbelt_spec(belt)}\n\n"
                  f"EVIDENCE GATHERED SO FAR:\n{obs}\n\n"
                  "Choose the next action as STRICT JSON.")
        try:
            resp = self.router.complete(ProblemType.FAST_PATH, system=SUPERVISOR_SYSTEM,
                                        prompt=prompt)
        except BudgetExceeded:
            return Action("diagnose", {}, "budget reached")
        j = _extract_json(resp.text)
        if not isinstance(j, dict) or "action" not in j:
            return None  # signals: model can't drive (e.g. offline mock) -> heuristic
        return Action(j["action"], j.get("args") or {}, j.get("rationale", ""))


class HeuristicPolicy:
    """Offline, state-adaptive. Reacts to evidence; not a fixed unrolled list."""
    BASELINE = ["read_timeline", "read_maintenance_history"]
    CLOG_PATH = ["search_manual", "check_parts", "check_safety_loto", "check_labor"]
    BROAD_PATH = ["check_recent_changes", "search_shift_notes", "read_production_econ"]
    ALWAYS = ["scan_ot_security"]

    def decide(self, ctx, belt, observations) -> Action:
        called = {n for n, _ in observations}
        # 1) baseline situational awareness
        for t in self.BASELINE:
            if t not in called:
                return Action(t, {}, "establish what happened and the maintenance history")
        # 2) adapt: did a clog signature show up?
        clog = any("clog" in e.claim.lower() or "high head pressure" in e.claim.lower()
                   or "low flow" in str(e.tags).lower() or "below baseline" in e.claim.lower()
                   for e in ctx.graph.items.values())
        order = self.CLOG_PATH if clog else self.BROAD_PATH
        for t in order:
            if t not in called:
                why = ("clog signature seen -> confirm procedure, parts, safety"
                       if clog else "no clear signature -> broaden the search")
                return Action(t, {}, why)
        # 3) cheap safety/security sweep, then the remaining broad/clog tools
        for t in self.ALWAYS + self.BROAD_PATH + self.CLOG_PATH:
            if t not in called and t in belt:
                return Action(t, {}, "complete coverage before deciding")
        return Action("diagnose", {}, "enough evidence gathered")


class AgenticGatherer:
    def __init__(self, max_steps: int = 12):
        self.max_steps = max_steps

    def run(self, ctx):
        belt = build_toolbelt(ctx)
        observations: list[tuple[str, str]] = []
        called: set[str] = set()
        stall = 0
        llm = LLMPolicy(ctx.router)
        heur = HeuristicPolicy()
        mode = None  # decided on first turn

        for step in range(self.max_steps):
            done, why = ctx.router.budget.exhausted()
            if done:
                ctx.log(f"agent: budget stop ({why}) -> diagnose")
                break
            if mode is None:
                a = llm.decide(ctx, belt, observations)
                mode = "heuristic-offline" if a is None else "llm-tooluse"
                ctx.tool_policy = mode
                if a is None:
                    a = heur.decide(ctx, belt, observations)
            else:
                a = (llm if mode == "llm-tooluse" else heur).decide(ctx, belt, observations)
                if a is None:
                    a = heur.decide(ctx, belt, observations)

            if a.action == "diagnose":
                ctx.log(f"agent[{mode}]: decided to DIAGNOSE — {a.rationale}")
                break
            tool = belt.get(a.action)
            if not tool:
                ctx.log(f"agent[{mode}]: unknown tool '{a.action}', diagnosing")
                break
            if a.action in called:                 # don't re-run; nudge to move on
                stall += 1
                observations.append((a.action, "already gathered (skipped)"))
                if stall >= 2:
                    ctx.log(f"agent[{mode}]: repeated tools -> diagnose")
                    break
                continue
            obs = tool.run(ctx, a.args)
            called.add(a.action)
            observations.append((a.action, obs))
            ctx.log(f"agent[{mode}] → {a.action}({a.args or ''}) :: {obs}"
                    + (f"  [why: {a.rationale}]" if a.rationale else ""))

        ctx.tools_called = list(dict.fromkeys(n for n, _ in observations
                                              if n in called))
        return HypothesisAgent().run(ctx)   # model-driven differential over what it gathered

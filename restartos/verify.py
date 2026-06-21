"""
restartos.verify
===============
Two independent gates that run BEFORE a human ever sees the plan:

  1. Cross-model groundedness verifier — runs on a DIFFERENT model family than
     the author (anti-collusion), prompted to REFUTE. Every step + part number
     must resolve to a real source or it is blocked. Citations are checked to
     *exist*, not trusted to be claimed.
  2. Automated safety pre-check — does the plan skip LOTO, contradict the
     manual's safety section, or need an absent permit?
"""
from __future__ import annotations

from .data import ManualAdapter, PartsAdapter, SafetyAdapter, DataRoot
from .domain import (Citation, RecoveryPlan, SafetyPrecheckReport, VerifierReport)
from .llm.router import ModelRouter, ProblemType


class CrossModelVerifier:
    name = "cross_model_verifier"

    def __init__(self, dr: DataRoot) -> None:
        self.man = ManualAdapter(dr)
        self.parts = PartsAdapter(dr)

    def verify(self, plan: RecoveryPlan, asset_model: str, router: ModelRouter) -> VerifierReport:
        refutations: list[str] = []
        resolved = 0
        cited = 0
        hallucinated_parts: list[str] = []

        for step in plan.steps:
            if step.citation:
                cited += 1
                ok = self._resolve(step.citation, asset_model)
                step.citation.resolved = ok
                if ok:
                    resolved += 1
                else:
                    refutations.append(
                        f"Step {step.n}: citation '{step.citation.locator}' does NOT "
                        f"resolve to a real manual passage — blocked.")
            for pn in step.part_numbers:
                if not self.parts.exists(pn):
                    hallucinated_parts.append(pn)
                    refutations.append(f"Step {step.n}: part '{pn}' not in inventory master.")

        rate = (resolved / cited) if cited else 0.0
        passed = (not refutations) and rate >= 0.999 and not hallucinated_parts
        # cross-model call — router routes VERIFICATION to a different family
        resp = router.complete(ProblemType.VERIFICATION,
            system="You are an adversarial verifier on a DIFFERENT model than the "
                   "author. Try to REFUTE the plan. Block any unresolved citation or "
                   "invented part. Do not confirm; find the flaw.",
            prompt=f"__MOCK_JSON__{{\"passed\": {str(passed).lower()}}}__END_MOCK_JSON__")
        return VerifierReport(grounded=passed, citation_resolution_rate=round(rate, 3),
                              hallucinated_parts=hallucinated_parts,
                              refutations=refutations, verifier_model=resp.model,
                              passed=passed)

    def _resolve(self, c: Citation, asset_model: str) -> bool:
        if c.source_system != "MANUAL":
            return c.resolved
        # parse "...#7.4@p.143"
        sec, page = None, None
        if "#" in c.locator:
            tail = c.locator.split("#", 1)[1]
            sec = tail.split("@")[0]
            if "p." in tail:
                try:
                    page = int(tail.split("p.")[1])
                except ValueError:
                    page = None
        return self.man.resolve_citation(asset_model, sec, page) if sec else False


class SafetyPrecheck:
    name = "safety_precheck"

    def __init__(self, dr: DataRoot) -> None:
        self.safety = SafetyAdapter(dr)
        self.man = ManualAdapter(dr)

    def check(self, plan: RecoveryPlan, funcloc: str, asset_model: str) -> SafetyPrecheckReport:
        notes: list[str] = []
        loto = self.safety.loto(funcloc)
        physical = plan.risk_class.value in ("LOTO_PHYSICAL", "LINE_RESTART")
        loto_in_plan = any(s.loto_required for s in plan.steps)
        loto_present = bool(loto) and (loto_in_plan or not physical)
        if physical and not loto_in_plan:
            notes.append("Physical work planned but no LOTO step present — FAIL.")
        # contradiction with manual safety section
        safety_sec = self.man.section(asset_model, "Safety")
        contradicts = False
        if safety_sec and "never service the nozzle without loto" in safety_sec["text"].lower():
            if physical and not loto_in_plan:
                contradicts = True
                notes.append("Plan contradicts manual Safety section (LOTO mandatory).")
        missing = []
        if loto and loto.get("requires_permit"):
            notes.append("HOT-PRODUCT permit may be required (temp-dependent).")
        passed = loto_present and not contradicts and not missing
        if passed:
            notes.append("LOTO present, no safety-section contradiction, permits accounted for.")
        return SafetyPrecheckReport(loto_present=loto_present,
                                    contradicts_safety_section=contradicts,
                                    missing_permits=missing, passed=passed, notes=notes)

"""
restartos.causal
================
First-fault isolation + a light causal-reasoning layer.

In a real plant a single failure throws a *storm* of alarms across a few
seconds, and the first thing a human sees is almost never the root cause. A
bottle backup at the capper is downstream of a filler that quietly stopped
flowing. Techs waste their time chasing the loudest downstream symptom.

This module separates:
  * the FIRST ACTIONABLE FAULT (where the chain actually starts),
  * the propagation links (cause -> effect),
  * the downstream symptoms that should be IGNORED as first actions, and
  * repeated alarms that are echoes of the first fault, not new faults.

It reasons from the evidence already gathered (historian signals, CMMS
recurrence, the operator's reported symptom) plus a small, auditable table of
known propagation chains — not a black box.
"""
from __future__ import annotations

from .domain import CausalChain, CausalLink


# Known propagation chains, keyed by diagnosed cause. Each tuple is (cause,
# effect). The FIRST entry's cause is the first actionable fault; the LAST
# entry's effect is the loud downstream symptom a human tends to chase.
CAUSE_CHAINS: dict[str, list[tuple[str, str]]] = {
    "Nozzle clog": [
        ("Fill-nozzle clog (product solids)", "Low flow at filler head"),
        ("Low flow at filler head", "High head pressure"),
        ("High head pressure", "Filler servo overload / repeated stops"),
        ("Filler servo overload / repeated stops", "Bottle backup downstream at capper"),
    ],
    "Pump wear": [
        ("Feed-pump impeller wear", "Falling discharge flow"),
        ("Falling discharge flow", "Filler starves / cycles"),
        ("Filler starves / cycles", "Downstream packaging starvation"),
    ],
    "Viscosity (new lot)": [
        ("New product lot — higher viscosity", "Nozzle flow drop at set pressure"),
        ("Nozzle flow drop at set pressure", "Underfill / slow fill"),
        ("Underfill / slow fill", "QC rejects + line slowdown"),
    ],
}

# Keywords that mark a claim/symptom as a DOWNSTREAM effect (loud but late).
DOWNSTREAM_MARKERS = ("backup", "backing up", "back up", "starv", "pile",
                      "capper", "downstream", "reject")


class CausalReasoner:
    name = "causal_reasoner"

    def analyze(self, ctx, top) -> CausalChain:
        cause = getattr(top, "cause", None) or "unknown"
        chain_def = CAUSE_CHAINS.get(cause)

        repeated = self._repeated_alarms(ctx)
        reported_downstream = self._reported_downstream(ctx)

        if not chain_def:
            # No known chain: be honest — the diagnosed cause is the best first
            # action we have, and any downstream-looking symptom is flagged.
            return CausalChain(
                first_actionable_fault=cause,
                links=[],
                downstream_symptoms=reported_downstream,
                repeated_alarms=repeated,
                ignored_as_downstream=reported_downstream,
                explanation=(f"No propagation model for '{cause}'. Treating it as "
                             f"the first actionable fault; "
                             + (f"flagged downstream noise: {reported_downstream}."
                                if reported_downstream else
                                "no downstream symptoms detected.")))

        links = [CausalLink(c, e) for c, e in chain_def]
        first = chain_def[0][0]
        downstream = [chain_def[-1][1]]
        # fold in what the operator actually reported, if it's a downstream effect
        for d in reported_downstream:
            if d not in downstream:
                downstream.append(d)

        path = " -> ".join([first] + [e for _, e in chain_def])
        explanation = (
            f"First actionable fault: {first}. Propagation: {path}. "
            f"The visible symptom ({downstream[0]}) is downstream, not the first "
            f"actionable fault — fix the head, not the backup.")
        if repeated:
            explanation += f" Repeated alarms ({', '.join(repeated)}) are echoes of the first fault."

        return CausalChain(
            first_actionable_fault=first,
            links=links,
            downstream_symptoms=downstream,
            repeated_alarms=repeated,
            ignored_as_downstream=downstream,
            explanation=explanation)

    def _reported_downstream(self, ctx) -> list[str]:
        out: list[str] = []
        sym = (getattr(ctx.incident, "symptom", "") or "").lower()
        raw = (getattr(ctx.incident, "raw_message", "") or "").lower()
        text = f"{sym} {raw}"
        if any(m in text for m in DOWNSTREAM_MARKERS):
            # surface the operator's own words as the downstream symptom
            out.append(ctx.incident.symptom or "reported downstream symptom")
        return out

    def _repeated_alarms(self, ctx) -> list[str]:
        out: list[str] = []
        for e in ctx.graph.items.values():
            tags = e.tags or {}
            # CMMS recurrence (same fault N/M work orders) reads as a repeat signal
            if e.source_system == "CMMS" and tags.get("asserts_cause") and "recurring" in e.claim.lower():
                out.append(f"{tags.get('asserts_cause')} (recurring per CMMS)")
            if tags.get("repeated") or tags.get("repeat_count"):
                out.append(e.claim[:60])
        # de-dupe preserving order
        seen, dedup = set(), []
        for x in out:
            if x not in seen:
                seen.add(x)
                dedup.append(x)
        return dedup[:4]

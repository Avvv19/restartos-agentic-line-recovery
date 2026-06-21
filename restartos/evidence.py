"""
restartos.evidence
=================
The Shared Evidence Graph. Specialist lenses WRITE timestamped facts here; the
hypothesis/planner READ from it. It does three things v1 architectures skip:

  1. Weights every claim by source trust x confidence x freshness-decay.
  2. Surfaces CONTRADICTIONS instead of averaging them away.
  3. Keeps full provenance so the verifier and the audit can trace any claim.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .domain import Contradiction, Evidence


@dataclass
class EvidenceGraph:
    items: dict[str, Evidence] = field(default_factory=dict)
    contradictions: list[Contradiction] = field(default_factory=list)

    def add(self, ev: Evidence) -> str:
        self.items[ev.evidence_id] = ev
        self._detect_contradictions(ev)
        return ev.evidence_id

    def add_many(self, evs: list[Evidence]) -> list[str]:
        return [self.add(e) for e in evs]

    def get(self, eid: str) -> Optional[Evidence]:
        return self.items.get(eid)

    def by_tag(self, key: str, value=None) -> list[Evidence]:
        out = []
        for e in self.items.values():
            if key in e.tags and (value is None or e.tags[key] == value):
                out.append(e)
        return out

    def support_for(self, hypothesis_key: str) -> list[Evidence]:
        return [e for e in self.items.values()
                if hypothesis_key in e.tags.get("supports", [])]

    def _detect_contradictions(self, ev: Evidence) -> None:
        """A claim that asserts the opposite cause on the same asset is a conflict."""
        for other in self.items.values():
            if other.evidence_id == ev.evidence_id:
                continue
            if other.tags.get("asset") == ev.tags.get("asset"):
                a_cause = ev.tags.get("asserts_cause")
                b_cause = other.tags.get("asserts_cause")
                if a_cause and b_cause and a_cause != b_cause \
                        and ev.tags.get("exclusive") and other.tags.get("exclusive"):
                    self.contradictions.append(Contradiction(
                        ev.evidence_id, other.evidence_id,
                        f"'{a_cause}' ({ev.source_system}) vs "
                        f"'{b_cause}' ({other.source_system})"))

    def weighted_score(self, eids: list[str]) -> float:
        return round(sum(self.items[e].weight() for e in eids if e in self.items), 4)

    def summary(self) -> dict:
        return {"n_items": len(self.items),
                "n_contradictions": len(self.contradictions),
                "sources": sorted({e.source_system for e in self.items.values()}),
                "total_weight": round(sum(e.weight() for e in self.items.values()), 3)}

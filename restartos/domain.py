"""
restartos.domain
================
Core domain model for RestartOS — the agentic line-recovery system.

Everything in the pipeline flows through these types. They are deliberately
plain dataclasses (stdlib only) so the critical path has zero heavy
dependencies and can be reasoned about, serialized, and audited cleanly.

Design rules encoded here:
  * Every Evidence item carries a source, trust, freshness and citation —
    a reading is evidence, not gospel.
  * Every Hypothesis carries calibrated confidence and the explicit
    "what would change my mind" counter-evidence.
  * The WorkPackage is the agent's OUTCOME, not a chat answer.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class Plane(str, Enum):
    """Purdue-aligned planes. The agent READS OT, WRITES IT. Never the reverse."""
    OT_CONTROL = "OT_CONTROL"     # L0-L2  sensors, PLC, SCADA/HMI  (READ ONLY)
    OT_OPS = "OT_OPS"             # L3     historian, MES           (READ ONLY)
    IT_BUSINESS = "IT_BUSINESS"   # L4     CMMS, ERP, QMS, notify    (WRITE, gated)


class Access(str, Enum):
    READ = "READ"
    WRITE = "WRITE"


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RiskClass(str, Enum):
    """Maps an action onto the authorization matrix."""
    WORK_ORDER_DRAFT = "WORK_ORDER_DRAFT"      # low      -> auto (logged)
    PARTS_RESERVE = "PARTS_RESERVE"            # low-med  -> maint planner
    LOTO_PHYSICAL = "LOTO_PHYSICAL"            # high     -> maintenance lead + e-sign
    RECIPE_CHANGE = "RECIPE_CHANGE"            # high     -> quality+process + 21 CFR 11
    LINE_RESTART = "LINE_RESTART"              # critical -> shift supervisor + e-sign


class Decision(str, Enum):
    ACT = "ACT"                       # confident + verified + safe -> gate
    ABSTAIN = "ABSTAIN"               # low confidence / contradiction -> escalate
    NEED_EVIDENCE = "NEED_EVIDENCE"   # inner loop: gather more, re-diagnose
    NEED_MORE_INFO = "NEED_MORE_INFO" # one specific human-supplied input unblocks a decision


class GateOutcome(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EDIT_REQUESTED = "EDIT_REQUESTED"
    PENDING = "PENDING"


@dataclass
class Incident:
    """The intake contract. A goal in — not a question."""
    asset_hint: str
    symptom: str
    line: str
    alarm_code: Optional[str] = None
    severity: Severity = Severity.HIGH
    downtime_rate_per_hr: float = 10_000.0
    started_at: float = field(default_factory=time.time)
    reported_by: str = "operator"
    incident_id: str = field(default_factory=lambda: f"INC-{uuid.uuid4().hex[:8]}")
    # Optional context lifted from a freeform operator message (see intake.py).
    machine_hint: Optional[str] = None
    product: Optional[str] = None
    deadline: Optional[str] = None
    safety_concern: Optional[str] = None
    raw_message: Optional[str] = None

    def downtime_minutes(self) -> float:
        return max(0.0, (time.time() - self.started_at) / 60.0)


@dataclass
class OperatorIntake:
    """Structured reading of a messy, free-text operator/maintenance report.

    The plant floor does not type clean API payloads. An operator writes
    "Line 3 filler keeps stopping after 20 min, bottles backing up at the
    capper, need a restart before 3 PM". This is what we pull out of that.
    """
    raw_message: str
    line: Optional[str] = None
    machine: Optional[str] = None
    symptom: Optional[str] = None
    alarm_code: Optional[str] = None
    urgency: Severity = Severity.HIGH
    product: Optional[str] = None
    deadline: Optional[str] = None
    safety_concern: Optional[str] = None
    missing_details: list[str] = field(default_factory=list)
    parsed_by: str = "intake"
    confidence: float = 0.0


@dataclass
class Citation:
    """A pointer that must RESOLVE to something real, not just be claimed."""
    source_system: str
    locator: str
    resolved: bool = False
    excerpt: Optional[str] = None


@dataclass
class Evidence:
    """One timestamped claim with provenance. Trust + freshness are first-class."""
    claim: str
    source_system: str
    plane: Plane
    citation: Optional[Citation] = None
    trust: float = 0.5
    freshness_s: float = 0.0
    confidence: float = 0.5
    tags: dict[str, Any] = field(default_factory=dict)
    produced_by: str = "system"
    ts: float = field(default_factory=time.time)
    evidence_id: str = field(default_factory=lambda: f"EV-{uuid.uuid4().hex[:8]}")

    def weight(self) -> float:
        """Effective weight = trust * confidence * freshness-decay (24h half-life)."""
        half_life = 86_400.0
        decay = 0.5 ** (self.freshness_s / half_life) if self.freshness_s > 0 else 1.0
        return round(self.trust * self.confidence * decay, 4)


@dataclass
class Contradiction:
    """Conflicts are surfaced, never averaged away."""
    a: str
    b: str
    note: str


@dataclass
class Hypothesis:
    cause: str
    iso14224_code: Optional[str] = None
    confidence: float = 0.0
    supporting: list[str] = field(default_factory=list)
    refuting: list[str] = field(default_factory=list)
    would_change_my_mind: str = ""


@dataclass
class ProcedureStep:
    n: int
    text: str
    citation: Optional[Citation] = None
    loto_required: bool = False
    part_numbers: list[str] = field(default_factory=list)


@dataclass
class RecoveryPlan:
    hypothesis: Hypothesis
    steps: list[ProcedureStep] = field(default_factory=list)
    est_minutes: int = 0
    techs_required: int = 1
    risk_class: RiskClass = RiskClass.WORK_ORDER_DRAFT
    parts: list[dict[str, Any]] = field(default_factory=list)
    residual_risk: str = ""


@dataclass
class VerifierReport:
    grounded: bool
    citation_resolution_rate: float
    hallucinated_parts: list[str] = field(default_factory=list)
    refutations: list[str] = field(default_factory=list)
    verifier_model: str = ""
    passed: bool = False


@dataclass
class SafetyPrecheckReport:
    loto_present: bool
    contradicts_safety_section: bool
    missing_permits: list[str] = field(default_factory=list)
    passed: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class CausalLink:
    """One edge in the fault propagation chain: cause -> effect."""
    cause: str
    effect: str
    note: str = ""


@dataclass
class CausalChain:
    """First-fault isolation. In a real plant one failure throws a storm of
    alarms; the first VISIBLE problem is usually downstream of the real one.
    This separates the first actionable fault from the noise it generated.
    """
    first_actionable_fault: str
    links: list[CausalLink] = field(default_factory=list)
    downstream_symptoms: list[str] = field(default_factory=list)
    repeated_alarms: list[str] = field(default_factory=list)
    ignored_as_downstream: list[str] = field(default_factory=list)
    explanation: str = ""

    def narrative(self) -> str:
        if self.explanation:
            return self.explanation
        path = " -> ".join([self.first_actionable_fault]
                           + [link.effect for link in self.links])
        return f"First actionable fault: {self.first_actionable_fault}. Chain: {path}."


@dataclass
class MaintenancePattern:
    """A repeat-failure signal mined from the CMMS work-order history."""
    asset_funcloc: str
    pattern: str                       # human-readable summary
    occurrences: int = 0
    window_days: int = 0
    repeated_part: Optional[str] = None
    repair_failed_within_h: Optional[float] = None
    symptom_only_fix: bool = False
    recommendation: str = ""


@dataclass
class KnowledgeCandidate:
    """Tribal knowledge lifted from shift notes — explicitly NOT yet trusted.

    Translating informal human know-how into something the next shift can use
    is the whole point. But an operator's hunch is not an OEM manual, so every
    candidate carries a trust status and the human step needed to promote it.
    """
    statement: str
    source: str
    status: str = "unverified"         # unverified | verified | stale | contradicted
    confirms_with: str = ""            # who must confirm before it becomes a playbook
    suggested_memory: str = ""


@dataclass
class MissingEvidenceRequest:
    """A NEED_MORE_INFO ask: the single input that would unblock a decision."""
    item: str                          # what is missing
    why: str                           # why it blocks the decision
    how_to_provide: str                # the concrete action the human takes
    unblocks: str = ""                 # what becomes possible once provided


@dataclass
class DecisionContract:
    """The final, trust-building contract emitted on EVERY run. It separates
    cleanly: what the agent knows, what it MAY do, what it is BLOCKED from
    doing, and what a human must approve.
    """
    incident_id: str
    decision: Decision
    allowed_next_action: str
    human_approval_required: bool
    forbidden_actions: list[str]
    approved_it_actions: list[str]
    evidence_used: list[str]
    missing_evidence: list[str]
    risk_class: str
    audit_id: str
    confidence: float = 0.0
    note: str = ""


@dataclass
class EscalationPacket:
    """A blocked run must still be USEFUL. A safe refusal that doesn't tell the
    next person what to do is worthless on a shop floor. This is the productive
    output of an ABSTAIN / NEED_MORE_INFO.
    """
    incident_id: str
    asset_funcloc: str
    operator_reported: str
    evidence_checked: list[str]
    evidence_missing: list[str]
    blocking_reason: str
    likely_cause: Optional[str]
    confidence: float
    route_to: str
    next_human_step: str
    contradictions: list[str] = field(default_factory=list)


@dataclass
class WorkPackage:
    """The agent's deliverable. Every item carries confidence + falsifier."""
    incident_id: str
    asset_funcloc: str
    likely_cause: Hypothesis
    evidence_trail: list[str]
    safe_checks: list[str]
    troubleshooting_path: list[ProcedureStep]
    work_order_draft: dict[str, Any]
    parts_request: list[dict[str, Any]]
    qc_sampling_plan: dict[str, Any]
    restart_readiness: list[str]
    shift_handover: str
    decision: Decision
    confidence: float
    economics: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    # Wave-1 additions — first-fault reasoning, mined patterns, captured
    # tribal knowledge, the structured handoff, and the final contract.
    causal_chain: Optional[CausalChain] = None
    maintenance_patterns: list[MaintenancePattern] = field(default_factory=list)
    knowledge_candidates: list[KnowledgeCandidate] = field(default_factory=list)
    shift_handoff: dict[str, Any] = field(default_factory=dict)
    decision_contract: Optional[DecisionContract] = None

    def artifacts(self) -> dict[str, Any]:
        """The package presented as the exact documents a human would otherwise
        produce by hand — named plainly so the value is obvious."""
        return {
            "root_cause_summary": {
                "cause": self.likely_cause.cause,
                "iso14224": self.likely_cause.iso14224_code,
                "confidence": self.confidence,
                "would_change_my_mind": self.likely_cause.would_change_my_mind,
                "first_actionable_fault": (self.causal_chain.first_actionable_fault
                                           if self.causal_chain else None),
            },
            "evidence_board": self.evidence_trail,
            "restart_checklist": self.restart_readiness,
            "work_order_draft": self.work_order_draft,
            "parts_reservation_request": self.parts_request,
            "technician_assignment": (self.work_order_draft or {}).get("tech"),
            "qc_sampling_plan": self.qc_sampling_plan,
            "shift_handoff_note": self.shift_handoff or {"text": self.shift_handover},
            "decision_contract": (to_jsonable(self.decision_contract)
                                  if self.decision_contract else None),
        }


def to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses/enums into JSON-serializable structures."""
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj

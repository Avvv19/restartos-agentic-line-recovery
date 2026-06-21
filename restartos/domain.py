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

    def downtime_minutes(self) -> float:
        return max(0.0, (time.time() - self.started_at) / 60.0)


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

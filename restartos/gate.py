"""
restartos.gate
=============
The human Authorization Gate — role-matched and risk-routed. Different actions
need different approvers and e-signatures (LOTO != recipe change != restart).
The gate sits between REASON and ACT; nothing writes to IT until it passes.

The gate is also ECONOMICS-ROUTED: a $50k/hr line justifies acting on lower
confidence than a $500/hr line. The required confidence floor scales inversely
with downtime cost (bounded).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional

import yaml  # type: ignore

from .domain import GateOutcome, RiskClass


@dataclass
class GateDecision:
    risk_class: str
    approver_role: str
    approver_identity: Optional[str]
    e_sign: bool
    outcome: GateOutcome
    rationale: str
    required_confidence: float
    actual_confidence: float


DEFAULT_MATRIX = {
    "WORK_ORDER_DRAFT": {"approver": "auto", "e_sign": False, "base_conf": 0.55},
    "PARTS_RESERVE":    {"approver": "maintenance_planner", "e_sign": False, "base_conf": 0.6},
    "LOTO_PHYSICAL":    {"approver": "maintenance_lead", "e_sign": True, "base_conf": 0.7},
    "RECIPE_CHANGE":    {"approver": "quality_process_eng", "e_sign": True, "base_conf": 0.8},
    "LINE_RESTART":     {"approver": "shift_supervisor", "e_sign": True, "base_conf": 0.75},
}


class AuthorizationGate:
    def __init__(self, matrix: Optional[dict] = None, config_path: Optional[str] = None) -> None:
        self.matrix = matrix or self._load(config_path) or DEFAULT_MATRIX

    @staticmethod
    def _load(path):
        if path and os.path.exists(path):
            return yaml.safe_load(open(path)).get("authorization_matrix")
        return None

    def required_confidence(self, risk_class: RiskClass, downtime_rate: float) -> float:
        base = self.matrix[risk_class.value]["base_conf"]
        # economics routing: scale the floor down as $/hr rises (bounded 0.5x..1x)
        factor = max(0.6, min(1.0, 5000.0 / max(1.0, downtime_rate)))
        return round(base * factor, 2)

    def evaluate(self, *, risk_class: RiskClass, confidence: float, downtime_rate: float,
                 safety_passed: bool, verifier_passed: bool,
                 approver: Callable[[GateDecision], GateOutcome] | None = None,
                 identity: str = "auto") -> GateDecision:
        spec = self.matrix[risk_class.value]
        req = self.required_confidence(risk_class, downtime_rate)
        # hard preconditions before a human is even asked
        if not (safety_passed and verifier_passed):
            return GateDecision(risk_class.value, spec["approver"], None, spec["e_sign"],
                                GateOutcome.REJECTED,
                                "auto-reject: safety/verifier gate not passed", req, confidence)
        if confidence < req:
            return GateDecision(risk_class.value, spec["approver"], None, spec["e_sign"],
                                GateOutcome.REJECTED,
                                f"confidence {confidence} < required {req} (economics-routed)",
                                req, confidence)
        # auto-approve low risk; otherwise require a human approver callback
        if spec["approver"] == "auto":
            return GateDecision(risk_class.value, "auto", "system", False,
                                GateOutcome.APPROVED, "low-risk auto-approval (logged)",
                                req, confidence)
        dec = GateDecision(risk_class.value, spec["approver"], identity, spec["e_sign"],
                           GateOutcome.PENDING, "awaiting role-matched human approval",
                           req, confidence)
        if approver is not None:
            dec.outcome = approver(dec)
            dec.rationale = f"{dec.approver_role} ({identity}) -> {dec.outcome.value}"
        return dec

"""
restartos.intake
================
The freeform operator intake layer.

A maintenance tech does not type a clean API payload while a line is bleeding
money. They write what they see:

    "Line 3 filler keeps stopping after 20 minutes, bottles are backing up near
     the capper, need a restart before 3 PM."

This module turns that messy sentence into a structured `OperatorIntake` —
line, machine, symptom, alarm, urgency, product, deadline, safety concern, and
crucially the *missing* details — and then hands a clean `Incident` to the
recovery engine.

Two drivers, same contract:
  * a deterministic heuristic parser that works fully offline (regex + a curated
    shop-floor vocabulary), and
  * an optional LLM pass that refines the heuristic when a model key is present.

The heuristic always runs first so the system never depends on a model being
available; the LLM only ever *improves* a field it is confident about.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from .domain import Incident, OperatorIntake, Severity


# --- shop-floor vocabulary ------------------------------------------------- #
MACHINES = [
    "filler", "capper", "labeler", "conveyor", "palletizer", "mixer", "pump",
    "sealer", "case packer", "case-packer", "depalletizer", "blender",
    "homogenizer", "cartoner", "wrapper", "checkweigher", "robot", "compressor",
    "chiller", "oven", "press", "cnc", "spindle", "filler head",
]

# symptom phrase -> normalized symptom
SYMPTOMS = [
    (r"backing up|back ?up|backup|pile ?up|piling", "product backing up"),
    (r"keeps? stopping|keeps? tripping|trips?|won'?t stay up|cycling", "intermittent stop"),
    (r"won'?t start|will not start|no start|dead", "fails to start"),
    (r"jam|jammed|jamming", "jam"),
    (r"leak|leaking|spray|dripping", "leak"),
    (r"low ?flow|under ?flow|slow fill|underfilling", "low flow"),
    (r"high (head )?pressure|over ?pressure", "high head pressure"),
    (r"overheat|too hot|burning smell|hot", "overheating"),
    (r"vibrat|shaking|rattl", "abnormal vibration"),
    (r"noise|grinding|squeal", "abnormal noise"),
    (r"down|stopped|not running|offline", "line down"),
]

SAFETY_TERMS = [
    (r"leak|spill|chemical|caustic|acid", "possible chemical/fluid release"),
    (r"smoke|burning|fire|spark", "possible fire / electrical hazard"),
    (r"hot|steam|scald|burn", "hot-surface / burn hazard"),
    (r"loto|lockout|tagout", "LOTO referenced"),
    (r"guard|interlock|pinch|crush", "guarding / pinch-point hazard"),
]

URGENT_TERMS = r"asap|urgent|now|immediately|critical|emergency|safety"

# product hints — generic; a plant ontology would extend this
PRODUCTS = ["bottle", "bottles", "can", "cans", "tea", "syrup", "juice", "water",
            "carton", "pouch", "case", "pallet", "tablet", "capsule", "vial"]


def _find_line(msg: str) -> Optional[str]:
    m = re.search(r"\bline\s*[-#]?\s*(\d+|[A-Z])\b", msg, re.I)
    if m:
        return f"Line {m.group(1)}"
    m = re.search(r"\b([A-Z]\d{1,2})[-\s]?(?:filler|line|cell)\b", msg, re.I)
    return m.group(1) if m else None


def _find_machine(msg: str) -> Optional[str]:
    low = msg.lower()
    # prefer the longest matching machine phrase
    for name in sorted(MACHINES, key=len, reverse=True):
        if name in low:
            return name.replace("-", " ")
    return None


def _find_alarm(msg: str) -> Optional[str]:
    m = re.search(r"\b([A-Z]{1,3}-?\d{2,4})\b", msg)
    # avoid grabbing pure times like "3 PM" or part numbers like 8200-NZ
    if m and not re.fullmatch(r"\d{2,4}", m.group(1)):
        cand = m.group(1)
        if "-" in cand and cand.split("-")[1].isalpha():
            return None  # looks like a part number (8200-NZ)
        return cand if "-" in cand else cand
    return None


def _find_symptom(msg: str) -> Optional[str]:
    low = msg.lower()
    hits = [norm for pat, norm in SYMPTOMS if re.search(pat, low)]
    if not hits:
        return None
    # de-dupe, keep order, join the two most specific
    seen: list[str] = []
    for h in hits:
        if h not in seen:
            seen.append(h)
    return "; ".join(seen[:2])


def _find_deadline(msg: str) -> Optional[str]:
    m = re.search(r"(?:before|by|until|no later than)\s+"
                  r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}:\d{2}|noon|midnight|eod|"
                  r"end of (?:shift|day))", msg, re.I)
    return m.group(0).strip() if m else None


def _find_product(msg: str) -> Optional[str]:
    low = msg.lower()
    for p in PRODUCTS:
        if re.search(rf"\b{re.escape(p)}\b", low):
            return p
    return None


def _find_safety(msg: str) -> Optional[str]:
    low = msg.lower()
    for pat, norm in SAFETY_TERMS:
        if re.search(pat, low):
            return norm
    return None


def _urgency(msg: str, deadline: Optional[str], safety: Optional[str]) -> Severity:
    low = msg.lower()
    if safety and "LOTO referenced" not in safety:
        return Severity.CRITICAL
    if re.search(URGENT_TERMS, low):
        return Severity.HIGH
    if deadline:
        return Severity.HIGH
    return Severity.MEDIUM


_LLM_SYSTEM = (
    "You convert a messy shop-floor incident report into STRICT JSON. Do not "
    "invent facts; if a field is not stated, use null. Keys: line, machine, "
    "symptom, alarm_code, product, deadline, safety_concern. "
    "alarm_code is a controller alarm like 'A-220' (never a part number or a "
    "time). Respond with the JSON object only.")


class IntakeParser:
    """Parse a freeform operator message into a structured OperatorIntake."""

    REQUIRED = ["line", "machine", "symptom", "alarm_code"]

    def parse(self, message: str, router=None) -> OperatorIntake:
        msg = (message or "").strip()
        deadline = _find_deadline(msg)
        safety = _find_safety(msg)
        intake = OperatorIntake(
            raw_message=msg,
            line=_find_line(msg),
            machine=_find_machine(msg),
            symptom=_find_symptom(msg),
            alarm_code=_find_alarm(msg),
            product=_find_product(msg),
            deadline=deadline,
            safety_concern=safety,
            urgency=_urgency(msg, deadline, safety),
            parsed_by="heuristic",
        )
        if router is not None:
            self._refine_with_model(intake, router)
        self._finalize(intake)
        return intake

    def _refine_with_model(self, intake: OperatorIntake, router) -> None:
        """Let a real model fill fields the heuristic missed. Offline mock
        replays the heuristic via __MOCK_JSON__ so behavior is deterministic."""
        from .llm.router import ProblemType
        seed = {"line": intake.line, "machine": intake.machine,
                "symptom": intake.symptom, "alarm_code": intake.alarm_code,
                "product": intake.product, "deadline": intake.deadline,
                "safety_concern": intake.safety_concern}
        try:
            resp = router.complete(
                ProblemType.FAST_PATH, system=_LLM_SYSTEM,
                prompt=(f"REPORT: {intake.raw_message}\n"
                        f"__MOCK_JSON__{json.dumps(seed)}__END_MOCK_JSON__"))
        except Exception:
            return
        from .agents import _extract_json
        j = _extract_json(resp.text)
        if not isinstance(j, dict):
            return
        # the model only fills blanks — it never overwrites a confident heuristic hit
        for f in ("line", "machine", "symptom", "alarm_code", "product",
                  "deadline", "safety_concern"):
            if not getattr(intake, f) and j.get(f):
                setattr(intake, f, str(j[f]))
        intake.parsed_by = f"heuristic+{getattr(resp, 'provider', 'model')}"

    def _finalize(self, intake: OperatorIntake) -> None:
        missing = [f for f in self.REQUIRED if not getattr(intake, f)]
        intake.missing_details = missing
        found = 4 - len(missing)
        intake.confidence = round(0.4 + 0.15 * found, 2)


def parse_message(message: str, router=None) -> OperatorIntake:
    return IntakeParser().parse(message, router=router)


def intake_to_incident(intake: OperatorIntake,
                       downtime_rate_per_hr: float = 10_000.0) -> Incident:
    """Bridge the structured intake into the engine's Incident contract.

    Falls back to sane, explicit placeholders when the operator left a field
    blank — those blanks are also recorded so the engine can ask for them
    (NEED_MORE_INFO) rather than guessing."""
    asset_hint = " ".join(p for p in (intake.line, intake.machine) if p) \
        or (intake.machine or intake.line or "unknown asset")
    return Incident(
        asset_hint=asset_hint,
        symptom=intake.symptom or "unspecified stop",
        line=intake.line or "unknown line",
        alarm_code=intake.alarm_code,
        severity=intake.urgency,
        downtime_rate_per_hr=downtime_rate_per_hr,
        machine_hint=intake.machine,
        product=intake.product,
        deadline=intake.deadline,
        safety_concern=intake.safety_concern,
        raw_message=intake.raw_message,
    )

"""
Wave-1 feature tests: freeform intake, NEED_MORE_INFO, decision contract,
first-fault isolation, maintenance pattern mining, tribal-knowledge capture,
and the escalation packet.

Run: PYTHONPATH=. python -m pytest tests/test_wave1.py -q
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from restartos.contracts import FORBIDDEN_ACTIONS
from restartos.data import CMMSAdapter, DataRoot
from restartos.domain import Decision, GateOutcome, Incident, Severity
from restartos.intake import intake_to_incident, parse_message
from restartos.orchestration import RestartOSEngine


def _engine():
    return RestartOSEngine()


def _approve(_d):
    return GateOutcome.APPROVED


# --- freeform intake -------------------------------------------------------- #
def test_intake_parses_messy_operator_report():
    it = parse_message("Line 3 filler keeps stopping after 20 min, bottles "
                       "backing up at the capper, A-220, restart before 3 PM")
    assert it.line == "Line 3"
    assert it.machine == "filler"
    assert it.alarm_code == "A-220"
    assert it.product == "bottles"
    assert it.deadline and "3" in it.deadline
    assert "backing up" in it.symptom
    assert it.missing_details == []          # all key fields present
    assert it.confidence >= 0.85


def test_intake_flags_missing_details():
    it = parse_message("something is off with the mixer")
    assert it.machine == "mixer"
    assert "alarm_code" in it.missing_details
    assert "line" in it.missing_details


def test_intake_alarm_not_confused_with_part_or_time():
    it = parse_message("replaced 8200-NZ on the capper at 3 PM, still jamming")
    assert it.alarm_code is None             # 8200-NZ is a part, 3 PM is a time


def test_intake_safety_escalates_urgency():
    it = parse_message("conveyor leaking caustic near line 4, smells burning")
    assert it.urgency == Severity.CRITICAL
    assert it.safety_concern


def test_intake_to_incident_bridges_fields():
    it = parse_message("Line 3 filler low flow, A-220, bottles")
    inc = intake_to_incident(it)
    assert isinstance(inc, Incident)
    assert inc.alarm_code == "A-220"
    assert inc.machine_hint == "filler"
    assert inc.raw_message


# --- decision modes + contract --------------------------------------------- #
def test_act_run_emits_full_decision_contract():
    res = _engine().run(Incident("Line 3 filler", "down", "Line 3", "A-220"),
                        approver=_approve)
    assert res.decision == Decision.ACT
    dc = res.decision_contract
    assert dc and dc["decision"] == "ACT"
    assert dc["human_approval_required"] is True
    assert dc["forbidden_actions"] == FORBIDDEN_ACTIONS
    assert any("CMMS" in a for a in dc["approved_it_actions"])
    assert dc["audit_id"]


def test_need_more_info_when_machine_named_but_line_missing():
    # Freeform report with a machine but no line → ask, don't refuse.
    inc = intake_to_incident(parse_message("the filler keeps stopping, bottles "
                                           "backing up at the capper"))
    res = _engine().run(inc, approver=_approve)
    assert res.decision == Decision.NEED_MORE_INFO
    assert res.missing_info and "filler" in res.missing_info["item"]
    assert res.decision_contract["decision"] == "NEED_MORE_INFO"
    assert res.escalation_packet["next_human_step"]


def test_unrecognized_alarm_code_is_not_silently_acted_on():
    # Strong sensor signature, but the operator's alarm code isn't in the OEM
    # fault map → the engine must ask to confirm, not silently ACT.
    res = _engine().run(Incident("Line 3 filler", "down", "Line 3", "A-999"),
                        approver=_approve)
    assert res.decision == Decision.NEED_MORE_INFO
    assert "A-999" in res.missing_info["item"]
    assert not res.it_actions  # nothing written


def test_abstain_unknown_asset_still_produces_escalation_packet():
    res = _engine().run(Incident("ghost machine xyz", "weird noise", "Line 9", None))
    assert res.decision == Decision.ABSTAIN
    ep = res.escalation_packet
    assert ep and ep["route_to"]
    assert ep["next_human_step"]
    assert res.decision_contract["decision"] == "ABSTAIN"


# --- first-fault isolation -------------------------------------------------- #
def test_first_fault_isolates_root_over_downstream_symptom():
    res = _engine().run(Incident("Line 3 filler", "bottles backing up at capper",
                                 "Line 3", "A-220"), approver=_approve)
    cc = res.causal_chain
    assert cc is not None
    assert "clog" in cc["first_actionable_fault"].lower()
    # the loud downstream symptom is explicitly NOT the first action
    downstream = " ".join(cc["downstream_symptoms"]).lower()
    assert "capper" in downstream or "backup" in downstream


# --- maintenance pattern mining -------------------------------------------- #
def test_pattern_mining_finds_repeat_failure_and_part():
    pats = CMMSAdapter(DataRoot()).patterns("FL-PKG-03-FILL")
    top = next(p for p in pats if p["cause"] == "Nozzle clog")
    assert top["occurrences"] >= 3
    assert top["repeated_part"] == "8200-NZ"      # CSV columns parsed correctly
    assert top["symptom_only_fix"] is True


def test_act_run_carries_patterns_and_tribal_knowledge():
    res = _engine().run(Incident("Line 3 filler", "down", "Line 3", "A-220"),
                        approver=_approve)
    assert res.maintenance_patterns
    assert res.knowledge_candidates
    # tribal knowledge is captured as UNVERIFIED until a lead confirms it
    assert all(k["status"] == "unverified" for k in res.knowledge_candidates)


def test_work_package_artifacts_are_named():
    res = _engine().run(Incident("Line 3 filler", "down", "Line 3", "A-220"),
                        approver=_approve)
    arts = res.work_package.artifacts()
    for key in ("root_cause_summary", "evidence_board", "restart_checklist",
                "work_order_draft", "parts_reservation_request",
                "qc_sampling_plan", "shift_handoff_note", "decision_contract"):
        assert key in arts
    assert arts["root_cause_summary"]["first_actionable_fault"]


# --- sufficiency, ROI, simulate-before-write -------------------------------- #
def test_evidence_sufficiency_high_on_full_data():
    res = _engine().run(Incident("Line 3 filler", "down", "Line 3", "A-220"),
                        approver=_approve)
    suff = res.evidence_sufficiency
    assert suff["score"] >= 85
    assert "Machine event timeline" not in suff["missing"]


def test_simulate_before_write_previews_all_writes():
    res = _engine().run(Incident("Line 3 filler", "down", "Line 3", "A-220"),
                        approver=_approve)
    sim = res.simulate_writes
    assert sim and sim["all_clear"] is True
    systems = {p["system"] for p in sim["planned"]}
    assert {"CMMS", "ERP", "QMS", "NOTIFY"} <= systems


def test_economics_has_risk_adjusted_value():
    res = _engine().run(Incident("Line 3 filler", "down", "Line 3", "A-220"),
                        approver=_approve)
    econ = res.work_package.economics
    assert econ["risk_adjusted_value_usd"] <= econ["value_per_event_usd"]
    assert econ["manual_paperwork_min_avoided"] > 0


# --- deterministic demo scenes ---------------------------------------------- #
def test_demo_builds_five_scenes_with_expected_decisions():
    from restartos.demo import build_all
    data = build_all(_engine())
    assert len(data["scenes"]) == 5
    by_id = {s["id"]: s for s in data["scenes"]}
    assert by_id[1]["run"]["decision"] == "ACT"
    assert by_id[2]["run"]["decision"] == "NEED_MORE_INFO"
    assert by_id[3]["run"]["decision"] == "ABSTAIN"
    assert any("REFUT" in t for t in by_id[4]["run"]["trace"])  # self-correction fired
    assert by_id[5]["kind"] == "ot_block"
    assert all(a["blocked"] for a in by_id[5]["attempts"])      # every OT write blocked

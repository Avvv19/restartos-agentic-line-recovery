"""
restartos.tools
==============
The agent's TOOLBELT — typed, permissioned calls over the plant's systems. The
orchestration supervisor (agent_loop.py) decides which of these to call and when.
Each tool:
  * declares a JSON schema (so a real model can call it via tool-use / JSON action)
  * is bound to a Purdue plane and is READ-ONLY against OT (writes happen only in
    the gated IT action plane, never here)
  * executes by running the matching specialist lens, which writes CITED evidence
    into the shared Evidence Graph, and returns a short observation string.

This is what makes it an agent rather than a fixed pipeline: the set of tools is
offered to the decision-maker, which selects them dynamically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import agents as A
from .domain import Access, Plane
from .security import assert_capability


@dataclass
class Tool:
    name: str
    description: str
    plane: Plane
    schema: dict
    run: Callable  # (ctx, args) -> observation str


def _wrap_lens(lens_cls, plane: Plane):
    def _run(ctx, args):
        assert_capability(plane, Access.READ)        # OT/IT read-only enforcement
        before = set(ctx.graph.items)
        lens_cls().run(ctx)
        new = [ctx.graph.items[i] for i in ctx.graph.items if i not in before]
        if not new:
            return "no new evidence"
        head = new[0].claim
        return f"+{len(new)} cited fact(s); e.g. {head[:120]}"
    return _run


def build_toolbelt(ctx) -> dict[str, Tool]:
    """Construct the tool registry. (Asset resolution already ran at intake.)"""
    specs = [
        ("read_timeline", A.TimelineAgent, Plane.OT_OPS,
         "Read SCADA/historian tag trends around the event (head pressure, flow, vibration). Read-only OT."),
        ("read_maintenance_history", A.MaintenanceAgent, Plane.IT_BUSINESS,
         "Read CMMS work-order history for this asset: recurring faults, MTBF, data conflicts."),
        ("search_manual", A.ManualAgent, Plane.IT_BUSINESS,
         "Semantic search the OEM manuals/SOPs for the alarm/symptom and return the cited procedure."),
        ("check_recent_changes", A.MOCAgent, Plane.IT_BUSINESS,
         "Read management-of-change records: setpoint edits, new product lots, recent PM ('what changed?')."),
        ("check_parts", A.PartsAgent, Plane.IT_BUSINESS,
         "Check MRO inventory on-hand, bin and lead time for likely repair parts."),
        ("check_safety_loto", A.SafetyAgent, Plane.IT_BUSINESS,
         "Check the LOTO procedure and permit requirements for this asset."),
        ("read_production_econ", A.ProductionEconAgent, Plane.OT_OPS,
         "Read MES downtime history and the downtime $/hr economic context."),
        ("check_labor", A.LaborAgent, Plane.IT_BUSINESS,
         "Check whether a qualified, LOTO-certified technician is on shift now."),
        ("search_shift_notes", A.ShiftNotesAgent, Plane.IT_BUSINESS,
         "Search free-text shift handovers for tribal-knowledge leads (low trust)."),
        ("scan_ot_security", A.SecurityAgent, Plane.IT_BUSINESS,
         "Scan connection configs for security defects (hardcoded secrets, TLS disabled)."),
    ]
    belt: dict[str, Tool] = {}
    for name, cls, plane, desc in specs:
        schema = {"type": "object", "properties": {}, "required": []}
        if name == "search_manual":
            schema["properties"] = {"query": {"type": "string",
                "description": "natural-language symptom/alarm to look up"}}
        belt[name] = Tool(name, desc, plane, schema, _wrap_lens(cls, plane))
    return belt


def toolbelt_spec(belt: dict[str, Tool]) -> str:
    """Render the toolbelt for a model prompt."""
    lines = []
    for t in belt.values():
        args = ", ".join(t.schema["properties"].keys()) or "none"
        lines.append(f"- {t.name}({args}) [{t.plane.value}, read-only]: {t.description}")
    return "\n".join(lines)

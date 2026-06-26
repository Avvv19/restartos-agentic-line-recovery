#!/usr/bin/env python3
"""
dataset/generate.py
===================
Generates a realistic, MESSY, multi-format industrial dataset for RestartOS.

Why messy on purpose? Because the whole value of the agent is joining across
silos that disagree, and reasoning under sensor drift, missing values,
contradictory records and tribal-knowledge shift notes. A clean dataset would
make the demo a lie (see kill-list #2 in the architecture).

Emits (>500 files) across formats:
  asset_registry.{csv,json}          curated cross-silo crosswalk (the keystone)
  historian/*.csv                     per-tag/day trend traces (OPC-UA/PI export style)
  mes/*.csv                           OEE / downtime reason codes / counts
  cmms/work_orders.csv + cmms/*.json  WO history with recurring faults + duplicates
  manuals/*.md + manuals/*.pdf        OEM Model 8200 manual + others (cited sources)
  parts/inventory.csv + parts/*.json  MRO stock, bins, lead times
  moc/*.json                          management-of-change records ("what changed?")
  safety/*.md                         LOTO procedures + permits
  shift_notes/*.md                    free-text handovers w/ slang + tribal knowledge
  config/*.ini                        system configs (one carries a planted secret)
  _manifest.json                      index + intentional-defect ledger (for evals)

Deterministic (seeded) so runs are reproducible.
"""
from __future__ import annotations

import csv
import json
import os
import random
import sys
from datetime import datetime, timedelta

SEED = 8200
random.seed(SEED)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.abspath(os.path.join(HERE, "..", "_data"))

DEFECTS: list[dict] = []          # intentional-defect ledger
FILES: list[str] = []


def _w(relpath: str, content: str, mode: str = "w") -> None:
    p = os.path.join(OUT, relpath)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, mode, encoding="utf-8",
              newline="" if relpath.endswith(".csv") else None) as f:
        f.write(content)
    FILES.append(relpath)


def defect(file: str, kind: str, note: str) -> None:
    DEFECTS.append({"file": file, "kind": kind, "note": note})


# --------------------------------------------------------------------------- #
# 1. ASSET REGISTRY — the keystone crosswalk (PI tag <-> SAP <-> manual <-> MES)#
# --------------------------------------------------------------------------- #
ASSETS = [
    # funcloc,              line,    pi_tags,                       mfg/model,        mes_id,    crit
    ("FL-PKG-03-FILL", "Line 3", ["4471", "4472", "4473"], "Acme Model 8200", "L3-FIL", "A"),
    ("FL-PKG-03-CAP",  "Line 3", ["4480", "4481"],         "Acme Model 6100", "L3-CAP", "B"),
    ("FL-PKG-03-LBL",  "Line 3", ["4490"],                 "Sato Model LX",   "L3-LBL", "C"),
    ("FL-PKG-02-FILL", "Line 2", ["3471", "3472"],         "Acme Model 8200", "L2-FIL", "A"),
    ("FL-PKG-01-CONV", "Line 1", ["2400", "2401"],         "Intralox ARB",    "L1-CNV", "C"),
    ("FL-UTL-00-AIR",  "Utility", ["1100"],                "Atlas GA75",      "UT-AIR", "A"),
    ("FL-PKG-04-FILL", "Line 4", ["5471", "5472", "5473"], "Acme Model 8200", "L4-FIL", "A"),
    ("FL-PKG-04-CAP",  "Line 4", ["5480", "5481"],         "Acme Model 6100", "L4-CAP", "B"),
    ("FL-PKG-05-FILL", "Line 5", ["6471", "6472", "6473"], "Krones VarioFill","L5-FIL", "A"),
    ("FL-PKG-05-PAL",  "Line 5", ["6490", "6491"],         "KUKA KR-210",     "L5-PAL", "B"),
]
TAG_MEANINGS = {
    "4471": ("fill_head_pressure", "bar"), "4472": ("fill_flow_rate", "L/min"),
    "4473": ("nozzle_vibration", "mm/s"),  "4480": ("cap_torque", "Nm"),
    "4481": ("cap_rate", "cpm"),           "4490": ("label_registration", "mm"),
    "3471": ("fill_head_pressure", "bar"), "3472": ("fill_flow_rate", "L/min"),
    "2400": ("conv_speed", "m/s"),         "2401": ("conv_motor_amps", "A"),
    "1100": ("air_pressure", "bar"),
    "5471": ("fill_head_pressure", "bar"), "5472": ("fill_flow_rate", "L/min"),
    "5473": ("nozzle_vibration", "mm/s"),  "5480": ("cap_torque", "Nm"),
    "5481": ("cap_rate", "cpm"),           "6471": ("fill_head_pressure", "bar"),
    "6472": ("fill_flow_rate", "L/min"),   "6473": ("nozzle_vibration", "mm/s"),
    "6490": ("pal_cycle_time", "s"),       "6491": ("pal_motor_amps", "A"),
}


def gen_asset_registry():
    rows = []
    for funcloc, line, tags, model, mes_id, crit in ASSETS:
        for t in (tags if isinstance(tags, list) else [tags]):
            rows.append({"pi_tag": t, "sap_funcloc": funcloc, "line": line,
                         "manufacturer_model": model, "mes_asset_id": mes_id,
                         "criticality": crit,
                         "tag_meaning": TAG_MEANINGS.get(t, ("unknown", ""))[0],
                         "uom": TAG_MEANINGS.get(t, ("", ""))[1]})
    # DEFECT: one funcloc deliberately mistyped in registry to test resolver fuzzing
    rows.append({"pi_tag": "4471", "sap_funcloc": "FL-PKG-3-FILL", "line": "Line 3",
                 "manufacturer_model": "Acme 8200", "mes_asset_id": "L3-FIL",
                 "criticality": "A", "tag_meaning": "fill_head_pressure", "uom": "bar"})
    defect("asset_registry.csv", "inconsistent_key",
           "Duplicate funcloc spelling FL-PKG-3-FILL vs FL-PKG-03-FILL; model 'Acme 8200' vs 'Acme Model 8200'")
    # CSV
    buf = ["pi_tag,sap_funcloc,line,manufacturer_model,mes_asset_id,criticality,tag_meaning,uom"]
    for r in rows:
        buf.append(",".join(str(r[k]) for k in
                   ["pi_tag", "sap_funcloc", "line", "manufacturer_model",
                    "mes_asset_id", "criticality", "tag_meaning", "uom"]))
    _w("asset_registry.csv", "\n".join(buf) + "\n")
    _w("asset_registry.json", json.dumps(rows, indent=2))


# --------------------------------------------------------------------------- #
# 2. HISTORIAN — per tag per day CSV traces (this dominates the file count)     #
# --------------------------------------------------------------------------- #
def gen_historian(days: int = 30):
    base = datetime(2026, 6, 2, 0, 0, 0)
    event_day = 28   # the A-220 event happens on day 14 (~09:14)
    for tag, (meaning, uom) in TAG_MEANINGS.items():
        for d in range(days):
            day = base + timedelta(days=d)
            rows = ["ts,tag,value,uom,quality"]
            # nominal baselines per meaning
            nominal = {"fill_head_pressure": 4.2, "fill_flow_rate": 38.0,
                       "nozzle_vibration": 2.1, "cap_torque": 12.0, "cap_rate": 220,
                       "label_registration": 0.3, "conv_speed": 1.2,
                       "conv_motor_amps": 18.0, "air_pressure": 6.5}.get(meaning, 1.0)
            for h in range(0, 24 * 60, 15):       # every 15 min
                ts = day + timedelta(minutes=h)
                val = nominal * (1 + random.uniform(-0.03, 0.03))
                q = "Good"
                # Inject the fault signature on the event day for the FILL asset tags
                if tag in ("4471", "4472", "4473") and d == event_day and 9 * 60 <= h <= 10 * 60:
                    if meaning == "fill_head_pressure":
                        val = nominal * 1.6      # head pressure UP (clog)
                    elif meaning == "fill_flow_rate":
                        val = nominal * 0.35     # flow DOWN (clog)
                    elif meaning == "nozzle_vibration":
                        val = nominal * 1.15     # mild vib (pump wear partial)
                # DEFECT: sensor drift on vibration tag over time (must be treated as evidence not gospel)
                if tag == "4473":
                    val += d * 0.04
                # DEFECT: stuck/frozen sensor for cap_torque after day 10
                if tag == "4480" and d >= 10:
                    val = 11.97
                    q = "Stale"
                # DEFECT: occasional bad-quality / missing rows
                if random.random() < 0.01:
                    rows.append(f"{ts.isoformat()},{tag},,{uom},Bad")
                    continue
                rows.append(f"{ts.isoformat()},{tag},{round(val,3)},{uom},{q}")
            _w(f"historian/{tag}_{day.strftime('%Y%m%d')}.csv", "\n".join(rows) + "\n")
    defect("historian/4473_*.csv", "sensor_drift", "nozzle_vibration drifts +0.04 mm/s per day")
    defect("historian/4480_*.csv", "frozen_sensor", "cap_torque frozen at 11.97 from day 10 (Stale quality)")
    defect("historian/*.csv", "missing_values", "~1% rows have empty value + Bad quality")


# --------------------------------------------------------------------------- #
# 3. MES / OEE                                                                  #
# --------------------------------------------------------------------------- #
DOWNTIME_CODES = {"DT-FILL-CLOG": "Filler nozzle clog", "DT-MECH": "Mechanical",
                  "DT-CHANGEOVER": "Changeover", "DT-QUAL": "Quality hold",
                  "DT-UNKNOWN": "Unknown/unclassified"}


def gen_mes():
    rows = ["date,line,mes_asset_id,good_count,reject_count,downtime_min,reason_code"]
    base = datetime(2026, 6, 2)
    for d in range(30):
        day = base + timedelta(days=d)
        for _, line, _, _, mes_id, _ in ASSETS:
            good = random.randint(40000, 52000)
            rej = random.randint(80, 400)
            dt = random.choice([0, 0, 0, 12, 25])
            code = random.choice(list(DOWNTIME_CODES)) if dt else ""
            if mes_id == "L3-FIL" and d == 28:
                dt, code, rej = 14, "DT-FILL-CLOG", 612    # the event
            rows.append(f"{day.date()},{line},{mes_id},{good},{rej},{dt},{code}")
    _w("mes/oee_daily.csv", "\n".join(rows) + "\n")
    # reason-code dictionary as XML (different format)
    xml = ['<?xml version="1.0"?>', "<downtime_codes>"]
    for c, desc in DOWNTIME_CODES.items():
        xml.append(f'  <code id="{c}">{desc}</code>')
    xml.append("</downtime_codes>")
    _w("mes/downtime_codes.xml", "\n".join(xml) + "\n")


# --------------------------------------------------------------------------- #
# 4. CMMS — work-order history with recurring faults + duplicates + contradiction#
# --------------------------------------------------------------------------- #
def gen_cmms(n: int = 220):
    base = datetime(2025, 1, 1)
    funclocs = [a[0] for a in ASSETS]
    causes = ["Nozzle clog", "Seal leak", "Bearing wear", "Sensor fault",
              "PLC fault", "Lubrication", "Belt tension", "Calibration drift"]
    header = ["wo_id", "funcloc", "open_date", "close_date", "cause", "action",
              "labor_hrs", "parts_used", "tech", "recurring_flag"]
    # Build with csv.writer so fields containing commas (e.g. the action text)
    # are properly quoted — otherwise downstream columns shift.
    import io as _io
    sio = _io.StringIO()
    w = csv.writer(sio)
    w.writerow(header)
    fill_clog_count = 0
    for i in range(n):
        wo = f"WO-{44000 + i}"
        fl = random.choice(funclocs)
        cause = random.choice(causes)
        # make nozzle clog recurring on the FILL asset (MTBF signal)
        if fl == "FL-PKG-03-FILL" and random.random() < 0.45:
            cause = "Nozzle clog"
            fill_clog_count += 1
        od = base + timedelta(days=random.randint(0, 520))
        cd = od + timedelta(hours=random.randint(1, 8))
        action = {"Nozzle clog": "Replaced nozzle kit 8200-NZ, flushed line",
                  "Seal leak": "Replaced seal 8200-SL",
                  "Bearing wear": "Replaced bearing 6204-2RS"}.get(cause, "Inspected/adjusted")
        parts = {"Nozzle clog": "8200-NZ", "Seal leak": "8200-SL",
                 "Bearing wear": "6204-2RS"}.get(cause, "")
        rec = "Y" if (fl == "FL-PKG-03-FILL" and cause == "Nozzle clog") else "N"
        w.writerow([wo, fl, od.date().isoformat(), cd.date().isoformat(),
                    cause, action, str(random.randint(1, 6)), parts,
                    random.choice(["jmartin", "kpatel", "rsingh", "lchen"]), rec])
    # DEFECT: a duplicate WO with a CONTRADICTORY cause (data conflict to surface)
    w.writerow(["WO-44999", "FL-PKG-03-FILL", "2026-05-30", "2026-05-30",
                "Operator error", "No fault found", "1", "", "jmartin", "N"])
    w.writerow(["WO-44999", "FL-PKG-03-FILL", "2026-05-30", "2026-05-30",
                "Nozzle clog", "Replaced 8200-NZ", "2", "8200-NZ", "kpatel", "Y"])
    defect("cmms/work_orders.csv", "duplicate_contradiction",
           "WO-44999 appears twice with contradictory cause (Operator error vs Nozzle clog)")
    _w("cmms/work_orders.csv", sio.getvalue())
    # a few detailed WO JSONs (different format)
    for i in range(8):
        wo = {"wo_id": f"WO-{44000+i}", "funcloc": "FL-PKG-03-FILL",
              "cause": "Nozzle clog", "notes": "Recurring on hot fill product; "
              "flush per Manual 8200 §7.4. Used nozzle kit 8200-NZ from bin A4.",
              "mttr_min": random.randint(30, 75)}
        _w(f"cmms/wo_{44000+i}.json", json.dumps(wo, indent=2))
    return fill_clog_count


# --------------------------------------------------------------------------- #
# 5. MANUALS — OEM Model 8200 (the cited source) + others; md + a real PDF      #
# --------------------------------------------------------------------------- #
MANUAL_8200 = {
    "1": "Model 8200 Volumetric Filler — Overview. Max head pressure 6.0 bar.",
    "7.1": "Section 7 Troubleshooting. Always isolate energy before service.",
    "7.4": ("§7.4 Low Flow / High Head Pressure (Alarm A-220). Probable cause: "
            "fill-nozzle clog from product solids. Procedure (LOTO REQUIRED): "
            "1) Apply lockout-tagout per plant LOTO-FILL-03. 2) Relieve line "
            "pressure. 3) Remove and inspect nozzle assembly. 4) Replace with "
            "nozzle kit 8200-NZ if scored or clogged. 5) Flush feed line. "
            "6) Reinstall, remove LOTO, run CIP, verify flow 36-40 L/min."),
    "8.2": "§8.2 Pump maintenance. Inspect bearing 6204-2RS at vibration > 4.5 mm/s.",
    "Safety": "Safety section: NEVER service the nozzle without LOTO. Hot product hazard.",
}

MANUAL_6100 = {
    "1": "Model 6100 Cap Tightener — Overview. Nominal torque 12.0 Nm, range 10.5-13.5 Nm.",
    "5.2": ("§5.2 Torque Out-of-Range (Alarm A-250). Probable cause: clutch slippage "
            "from worn drive belt or contaminated chuck. Procedure (LOTO REQUIRED): "
            "1) Apply lockout-tagout per plant LOTO-CAP-03. 2) Inspect drive belt "
            "tension; replace belt 6100-BELT if frayed or stretched > 4%. "
            "3) Clean chuck surfaces with isopropyl. 4) Calibrate torque transducer. "
            "5) Run 20 sample bottles, verify torque 11.0-13.0 Nm range."),
    "Safety": "Safety section: cap turret hazard — LOTO before opening guard.",
}

MANUAL_INTRALOX = {
    "1": "Intralox ARB Conveyor — Overview. Nominal belt speed 1.2 m/s.",
    "3.1": ("§3.1 Conveyor Motor Trip (Alarm A-310). Probable cause: jammed product, "
            "obstruction on belt, or motor over-current. Procedure (LOTO REQUIRED): "
            "1) Apply lockout-tagout per plant LOTO-CONV-01. 2) Visually inspect belt "
            "for product spillage, broken bottles, or foreign objects. 3) Manually "
            "rotate belt to confirm free movement. 4) If motor amps remain high, "
            "swap motor contactor MC-2400. 5) Reset, verify motor amps < 22 A nominal."),
    "Safety": "Safety section: pinch-point hazard at belt entry; full LOTO required.",
}

MANUAL_ATLAS = {
    "1": "Atlas GA75 Air Compressor — Overview. Set point 6.5 bar, alarm threshold 5.8 bar.",
    "4.5": ("§4.5 Low Air Pressure (Alarm A-410). Probable cause: filter saturation "
            "(leading indicator) or downstream leak. Procedure (NO LOTO REQUIRED for "
            "filter swap): 1) Check filter differential pressure gauge. 2) If > 0.4 bar "
            "delta, replace filter element GA75-FILT. 3) If filter clean, walk down "
            "ring main; listen for leaks at unions and quick-disconnects. 4) Recheck "
            "set point recovery within 90 seconds."),
    "Safety": "Safety section: pressurised line; relieve before any disconnection.",
}


def gen_manuals():
    # Model 8200 manual as markdown, page-addressable
    md = ["# Acme Model 8200 Volumetric Filler — Service Manual", ""]
    page_map = {"1": 1, "7.1": 140, "7.4": 143, "8.2": 167, "Safety": 9}
    for sec, txt in MANUAL_8200.items():
        md.append(f"## §{sec}  (p.{page_map[sec]})\n\n{txt}\n")
    _w("manuals/Acme_Model_8200.md", "\n".join(md))

    # Model 6100 Cap Tightener — alarm A-250 (cap torque out-of-range)
    md6100 = ["# Acme Model 6100 Cap Tightener — Service Manual", ""]
    pages = {"1": 1, "5.2": 72, "Safety": 8}
    for sec, txt in MANUAL_6100.items():
        md6100.append(f"## §{sec}  (p.{pages[sec]})\n\n{txt}\n")
    _w("manuals/Acme_Model_6100.md", "\n".join(md6100))

    # Intralox ARB Conveyor — alarm A-310 (motor trip)
    mdi = ["# Intralox ARB Conveyor — Service Manual", ""]
    pi = {"1": 1, "3.1": 38, "Safety": 6}
    for sec, txt in MANUAL_INTRALOX.items():
        mdi.append(f"## §{sec}  (p.{pi[sec]})\n\n{txt}\n")
    _w("manuals/Intralox_ARB.md", "\n".join(mdi))

    # Atlas GA75 air compressor — alarm A-410 (low air pressure)
    mda = ["# Atlas GA75 Air Compressor — Service Manual", ""]
    pa = {"1": 1, "4.5": 55, "Safety": 7}
    for sec, txt in MANUAL_ATLAS.items():
        mda.append(f"## §{sec}  (p.{pa[sec]})\n\n{txt}\n")
    _w("manuals/Atlas_GA75.md", "\n".join(mda))

    # Remaining stub manuals (breadth)
    for model, secs in {"Sato_LX": 22}.items():
        body = [f"# {model} Manual"]
        for s in range(1, secs):
            body.append(f"## §{s}\nRoutine content for {model} section {s}.")
        _w(f"manuals/{model}.md", "\n".join(body))
    # a real PDF of the 8200 §7.4 (cited page) if reportlab is available
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
        p = os.path.join(OUT, "manuals/Acme_Model_8200_p143.pdf")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        c = canvas.Canvas(p, pagesize=letter)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(72, 720, "Acme Model 8200 — Service Manual  (p.143)")
        c.setFont("Helvetica", 10)
        y = 690
        for line in _wrap(MANUAL_8200["7.4"], 90):
            c.drawString(72, y, line)
            y -= 16
        c.save()
        FILES.append("manuals/Acme_Model_8200_p143.pdf")
    except Exception as e:
        defect("manuals/Acme_Model_8200_p143.pdf", "skipped", f"reportlab unavailable: {e}")


def _wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        out.append(line)
    return out


# --------------------------------------------------------------------------- #
# 6. PARTS / INVENTORY                                                          #
# --------------------------------------------------------------------------- #
def gen_parts():
    rows = ["part_no,description,on_hand,bin,lead_time_days,unit_cost"]
    catalog = [("8200-NZ", "Nozzle kit Model 8200", 2, "A4", 5, 145.00),
               ("8200-SL", "Seal kit Model 8200", 0, "A5", 14, 38.50),
               ("6204-2RS", "Bearing 6204-2RS", 11, "C2", 3, 9.25),
               ("CIP-CHEM", "CIP flush concentrate 20L", 4, "D1", 7, 60.0),
               # parts for the new alarm scenarios
               ("6100-BELT", "Drive belt Model 6100 cap tightener", 3, "B2", 7, 78.00),
               ("MC-2400", "Motor contactor 24A Intralox conveyor", 5, "B6", 3, 145.00),
               ("GA75-FILT", "Air filter element Atlas GA75", 8, "D3", 2, 42.50)]
    for p in catalog:
        rows.append(",".join(str(x) for x in p))
    # breadth: 120 misc parts
    for i in range(120):
        rows.append(f"GEN-{1000+i},Generic spare {i},{random.randint(0,50)},"
                    f"{random.choice('ABCD')}{random.randint(1,9)},"
                    f"{random.randint(1,30)},{round(random.uniform(2,500),2)}")
    # DEFECT: a part referenced by manuals/CMMS but MISSING from inventory (must abstain/flag)
    defect("parts/inventory.csv", "missing_master_data",
           "Part 8200-NZ has only 2 on hand; seal 8200-SL is 0 on hand (lead 14d)")
    _w("parts/inventory.csv", "\n".join(rows) + "\n")
    _w("parts/bins.json", json.dumps({"A4": "Aisle A Rack 4 (fast-movers)",
                                      "A5": "Aisle A Rack 5"}, indent=2))
    try:
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active; ws.title = "MRO"
        ws.append(["part_no", "description", "on_hand", "bin", "lead_time_days", "unit_cost"])
        for line in rows[1:]:
            ws.append(line.split(","))
        xp = os.path.join(OUT, "parts/inventory.xlsx")
        wb.save(xp); FILES.append("parts/inventory.xlsx")
    except Exception as e:
        defect("parts/inventory.xlsx", "skipped", f"openpyxl unavailable: {e}")


# --------------------------------------------------------------------------- #
# 7. MOC — management of change ("what changed?")                               #
# --------------------------------------------------------------------------- #
def gen_moc():
    recs = [
        {"moc_id": "MOC-2026-051", "date": "2026-06-30T09:12:00", "line": "Line 3",
         "funcloc": "FL-PKG-03-FILL", "change": "New product lot LOT-7741 introduced; "
         "viscosity spec 1.8x prior lot", "approved_by": "process.eng",
         "risk_review": "pending"},
        {"moc_id": "MOC-2026-047", "date": "2026-06-24T14:00:00", "line": "Line 3",
         "funcloc": "FL-PKG-03-FILL", "change": "PM completed: nozzle inspection, "
         "no parts replaced", "approved_by": "maint.lead", "risk_review": "closed"},
    ]
    for r in recs:
        _w(f"moc/{r['moc_id']}.json", json.dumps(r, indent=2))
    # breadth
    for i in range(30):
        r = {"moc_id": f"MOC-2026-{i:03d}", "date": "2026-05-01T00:00:00",
             "line": random.choice(["Line 1", "Line 2"]), "change": f"Minor change {i}",
             "risk_review": "closed"}
        _w(f"moc/MOC-2026-{i:03d}.json", json.dumps(r, indent=2))
    defect("moc/MOC-2026-051.json", "open_risk", "Recent viscosity change with risk_review=pending — prime suspect")


# --------------------------------------------------------------------------- #
# 8. SAFETY / LOTO                                                              #
# --------------------------------------------------------------------------- #
def gen_safety():
    _w("safety/LOTO-FILL-03.md",
       "# LOTO-FILL-03 — Filler FL-PKG-03-FILL\n\n"
       "Energy sources: electrical (MCC-3 breaker 7), pneumatic (air valve AV-3), "
       "hydraulic (product line).\n\nSteps: 1) Notify operator. 2) Stop line. "
       "3) Lock MCC-3 brk 7. 4) Bleed AV-3. 5) Relieve product line pressure. "
       "6) Verify zero energy. 7) Apply personal locks.\n\n"
       "Permit required: HOT-PRODUCT permit if temp > 60C.\n")
    for fl in ["FL-PKG-03-CAP", "FL-PKG-02-FILL", "FL-PKG-01-CONV"]:
        _w(f"safety/LOTO-{fl}.md", f"# LOTO for {fl}\nStandard isolation procedure.\n")


# --------------------------------------------------------------------------- #
# 9. SHIFT NOTES — tribal knowledge + slang (localization challenge)            #
# --------------------------------------------------------------------------- #
SLANG = [
    "Filler been spitting all arvo, nozzle's prob gummed again — same as last graveyard shift.",
    "L3 filler chucked a wobbly ~09:15, head pressure pinned, flow tanked. Smells like a clog.",
    "Heads up next shift: new product lot run since brekkie, way thicker, watch the filler.",
    "Pump's a bit growly but she'll be right, keep an eye on the vibbo trend.",
    "Did a quick CIP flush, nozzle looked manky. Swapped from bin A4. Logged as WO.",
]


def gen_shift_notes():
    base = datetime(2026, 6, 2)
    for d in range(30):
        day = base + timedelta(days=d)
        for shift in ["day", "night"]:
            note = random.choice(SLANG)
            if d == 28 and shift == "day":
                note = ("09:20 — L3 filler down, alarm A-220. Head pressure pinned high, "
                        "flow dropped to ~13 L/min. New lot LOT-7741 started 09:12, real "
                        "thick stuff. Reckon nozzle's clogged again — third time this month. "
                        "Flagged for maint, did NOT touch it (needs LOTO).")
            _w(f"shift_notes/{day.date()}_{shift}.md",
               f"# Shift handover {day.date()} ({shift})\n\n{note}\n")
    defect("shift_notes/*.md", "unstructured_slang",
           "Tribal knowledge buried in shift slang ('chucked a wobbly', 'vibbo', 'manky')")


# --------------------------------------------------------------------------- #
# 10. CONFIG — with a PLANTED security defect (hardcoded credential)            #
# --------------------------------------------------------------------------- #
def gen_hr_roster():
    """Plant HR roster + certifications. Shape mirrors a BambooHR/Workday extract.
    Used by the LaborLens agent to pick a qualified, on-shift technician."""
    rows_csv = ["employee_id,name,role,line,shift,cert_loto,cert_mech_l2,cert_electrical,cert_compressor,phone"]
    roster = [
        # employee_id,    name,           role,                    line,     shift,  loto, mech-L2, electrical, compressor, phone
        ("E-1001", "jmartin",  "Sr. Mechanical Tech",   "Line 3", "B",    "Y", "Y", "N", "N", "+1-555-0101"),
        ("E-1002", "kpatel",   "Maintenance Lead",      "Line 3", "B",    "Y", "Y", "Y", "N", "+1-555-0102"),
        ("E-1003", "rsingh",   "Mechanical Tech",       "Line 4", "A",    "Y", "Y", "N", "N", "+1-555-0103"),
        ("E-1004", "lchen",    "Electrical Tech",       "Line 2", "B",    "Y", "N", "Y", "N", "+1-555-0104"),
        ("E-1005", "dwilliams","Utility Specialist",    "Utility","A",    "Y", "N", "Y", "Y", "+1-555-0105"),
        ("E-1006", "nrodriguez","Mechanical Tech",      "Line 5", "C",    "Y", "Y", "N", "N", "+1-555-0106"),
        ("E-1007", "akumar",   "Operator",              "Line 1", "B",    "N", "N", "N", "N", "+1-555-0107"),
        ("E-1008", "bnguyen",  "Shift Supervisor",      "Plant",  "B",    "Y", "Y", "Y", "N", "+1-555-0108"),
        ("E-1009", "tokonkwo", "Operator",              "Line 3", "B",    "N", "N", "N", "N", "+1-555-0109"),
        ("E-1010", "shasan",   "Operator",              "Line 4", "A",    "N", "N", "N", "N", "+1-555-0110"),
    ]
    for r in roster:
        rows_csv.append(",".join(r))
    _w("hr/shift_roster.csv", "\n".join(rows_csv) + "\n")
    # JSON form for the LaborLens
    _w("hr/shift_roster.json", json.dumps([dict(zip(
        ["employee_id", "name", "role", "line", "shift", "cert_loto", "cert_mech_l2",
         "cert_electrical", "cert_compressor", "phone"], r)) for r in roster], indent=2))


def gen_configs():
    _w("config/historian_connection.ini",
       "[pi_web_api]\nhost = pi.plant.local\nport = 443\n"
       "# SECURITY DEFECT (planted): credentials must never be hardcoded\n"
       "username = svc_historian\npassword = P@ssw0rd-2024!\n"
       "verify_ssl = false\n")
    defect("config/historian_connection.ini", "hardcoded_secret",
           "Plaintext password + verify_ssl=false — flagged by security scanner")
    _w("config/cmms_connection.ini",
       "[sap_pm]\nhost = sap.plant.local\nclient = 100\n"
       "auth = ${SAP_TOKEN}   ; correct: read from env, not hardcoded\n")


# --------------------------------------------------------------------------- #
def main():
    global OUT
    if len(sys.argv) > 1:
        OUT = os.path.abspath(sys.argv[1])
    os.makedirs(OUT, exist_ok=True)
    gen_asset_registry()
    gen_historian()
    gen_mes()
    clog = gen_cmms()
    gen_manuals()
    gen_parts()
    gen_moc()
    gen_safety()
    gen_shift_notes()
    gen_hr_roster()
    gen_configs()
    manifest = {
        "generated_at": datetime.now().isoformat(),
        "seed": SEED,
        "scenario": "Line 3 Filler FL-PKG-03-FILL, alarm A-220, new lot LOT-7741",
        "file_count": len(FILES),
        "formats": sorted({os.path.splitext(f)[1] for f in FILES}),
        "recurring_fill_clog_wos": clog,
        "intentional_defects": DEFECTS,
        "files": sorted(FILES),
    }
    _w("_manifest.json", json.dumps(manifest, indent=2))
    print(f"[generate] wrote {len(FILES)} files to {OUT}")
    print(f"[generate] formats: {manifest['formats']}")
    print(f"[generate] intentional defects: {len(DEFECTS)}")


if __name__ == "__main__":
    main()

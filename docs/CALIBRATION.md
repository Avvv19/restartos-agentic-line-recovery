# 🟡 The 3-4 Week Calibration Window

**Before Restart OS is allowed to autonomously create a work order on your real production line, it must spend 3-4 weeks in *shadow mode* on your plant's own incidents, with a maintenance lead reviewing every proposal.**

This document explains, in plain English, why that window exists, what happens during it, what you watch for, and when (and only when) the gate is allowed to flip from "shadow" to "live."

This is not a marketing recommendation. It is the protocol the system itself enforces. The engine refuses to perform autonomous dispatch until the calibration boolean in `shadow_report.json` is `true`.

---

## Why a calibration window is non-negotiable

A general-purpose AI model has never seen *your* plant. It has read OEM manuals, it has read public maintenance literature, it has structured reasoning about pumps and bearings — but it has never met:

- Your specific shift culture (what "real thick stuff" means in your supervisor's shift notes)
- Your specific tribal workarounds (the time Bob fixed it by tapping the regulator with the back of a wrench)
- Your specific cert mappings (whether "mech-L2" at your plant means the same as the BambooHR field we ship with)
- Your specific tolerance for ambiguity (some lines lose $500/hr down, some lose $50,000/hr — the gate threshold has to be tuned)
- Your specific safety culture (one site's LOTO permit takes 12 minutes, another's takes 45)

Until the system has watched 3-4 weeks of real incidents and been compared to what a real human did on each one, **its agreement rate with your team is unknown**. An unknown agreement rate is not safe to act on. Period.

---

## What "shadow mode" actually means

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  PLC fault fires  ─────►  Restart OS engine processes it  ───┐   │
│                                                              │   │
│                          ┌────────────────────────────────┐  │   │
│                          │  Agent's proposed Recovery     │◄─┘   │
│                          │  Work Package (full plan,      │      │
│                          │  cause, parts, tech, MTTR)     │      │
│                          └────────────────┬───────────────┘      │
│                                           │                      │
│         ╔═════════════════════════════════▼═════════════════╗    │
│         ║         🔒  HUMAN REVIEWER (maintenance lead)     ║    │
│         ║   reads the proposal, AND reads what they would   ║    │
│         ║   actually have done, AND scores agreement        ║    │
│         ╚════╤════════════════════════════════════════╤════╝     │
│              │  proposal goes to a queue,             │          │
│              │  NEVER to the live CMMS                │          │
│              ▼                                        ▼          │
│      ┌───────────────┐                       ┌───────────────┐   │
│      │ Audit log     │                       │ Daily report  │   │
│      │ every run     │                       │ to ops team   │   │
│      └───────────────┘                       └───────────────┘   │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

The engine runs **identically** to live mode. It calls every tool, it diagnoses, it plans, it verifies, it gates. The only difference is one configuration flag — `RESTARTOS_LIVE=0` — which causes the IT writes to land in a quarantined JSON store instead of your real CMMS/ERP/Slack.

Your maintenance lead sees the agent's proposal on the cockpit alongside what they would have done. The discrepancy is captured. After 3-4 weeks, the discrepancies are summarized into a single agreement rate.

---

## The four numbers that matter

Each week the harness reports four metrics. All four must clear their thresholds for the system to graduate to live.

| Metric | What it means | Target | Why it matters |
|---|---|---|---|
| **decision_agreement_rate** | When the agent says ACT vs ABSTAIN, does the human agree? | ≥ 85 % | A 60 % agreement rate would mean we disagree with the human 4 times out of 10 — not safe. |
| **cause_top1_agreement_rate** | When both ACT, does the agent's top root cause match the human's? | ≥ 80 % | The wrong root cause sends the wrong technician with the wrong parts — that's worse than waiting. |
| **silent_wrong_act_rate** | Cases where the agent ACTed with the WRONG cause and the human had to override. | = 0.0 % | The one outcome the system must never produce. A single instance kills the pilot. |
| **parts_jaccard_mean** | Overlap between the parts the agent reserved and the parts the human used. | ≥ 0.70 | If the agent reserves the wrong parts, the tech walks to the wrong bin and adds 30 minutes per call. |

The harness writes these to `_it_state/shadow_report.json`. The `pass_for_production` boolean at the bottom of that file is **the** decision criterion. The plant manager looks at one number, not four.

---

## Week-by-week protocol

### Week 1 — Onboarding and instrumentation
- Run `python -m restartos.shadow_mode --labels <your_50_historical_incidents>.jsonl` on **historical** closed work orders, to bootstrap a baseline.
- Wire the live PI/Historian credentials in `.env` (read-only).
- Wire Slack to a **shadow channel** that only your maintenance lead reads.
- Maintenance lead spends ~10 min per incident reading the agent's proposal.
- Daily review meeting: 15 min, agreement rate trending.

### Week 2 — Calibration of disagreement
For each disagreement, classify it:
- **Agent-correct, human-wrong** → keep the agent's call; update the maintenance team's procedure.
- **Human-correct, agent-wrong (cause)** → add the case to `eval/incidents_labeled.jsonl` for regression coverage; tune the verifier prompt or the evidence trust weights.
- **Human-correct, agent-wrong (parts)** → review the OEM manual chunks in Qdrant; ingest any missing supplementary docs.
- **Both correct, different paths** → record as "acceptable variance"; no action.

### Week 3 — Plant-specific cert mapping
- Map your HRIS custom fields to Restart OS cert keys (see `connectors.py:BambooHRIS.DEFAULT_CERT_MAP`).
- Validate that the `LaborAgent` picks the right tech for each alarm type. Maintenance lead reviews 5 randomly-selected picks per shift.
- Verify Slack pages reach the right tech's phone in <60 s.

### Week 4 — Boundary review
- Maintenance lead audits the **abstention** cases: was every refusal documented with a reason? Did the agent abstain on anything where the human would have acted?
- Compliance review of the audit log against the plant's IEC 62443 / NERC CIP requirements.
- Final agreement rate is computed across all 4 weeks.

### Graduation
If **all four numbers** clear their thresholds at the end of week 4, the gate may flip from `RESTARTOS_LIVE=0` to `RESTARTOS_LIVE=1`. The agent now creates real work orders in the real CMMS, but the human approval gate stays in place. **Autonomous gate-bypass is a separate, much later step** — typically another 4-8 weeks of "live with mandatory human approval" before any conversation about reducing the approval requirement.

If any number is below threshold:
- Identify the largest single contributor to the gap
- Tune (prompt, threshold, verifier strictness, missing OEM doc)
- Re-run shadow mode for 1 more week
- Re-evaluate

---

## What you DON'T have to do

To make this manageable for plant operations teams that don't have an AI specialist:

- ❌ You don't have to write code. Calibration is all configuration + review.
- ❌ You don't have to label thousands of incidents. 50 historical labels for the bootstrap, plus the natural flow of 3-4 weeks of new incidents, is enough.
- ❌ You don't have to run a separate ML pipeline. The same `python -m restartos.shadow_mode` command produces the report.
- ❌ You don't have to interpret raw model output. The cockpit shows the agent's proposal in plain English with citations.

---

## How to run shadow mode yourself

```bash
# 1. Have RESTARTOS_LIVE=0 in your .env (this is the default).
# 2. Point the engine at your real PI / HRIS via PI_BASE_URL, BAMBOO_*.
#    CMMS_BASE_URL stays unset so writes go to the local quarantine.
# 3. Export your last 50 closed work orders to JSONL in this format:
#    {"incident_id":"...","asset_hint":"...","line":"...","alarm_code":"...",
#     "downtime_rate":...,"severity":"HIGH","ground_truth":
#     {"decision":"ACT","cause":"...","parts":["..."],"mttr_min":...,"tech":"..."}}

docker compose up -d
docker exec restartos-engine \
    python -m restartos.shadow_mode \
    --labels /app/eval/your_incidents.jsonl \
    --out /app/_it_state/shadow_report.json

# Read the verdict:
docker exec restartos-engine cat /app/_it_state/shadow_report.json \
    | python -c "import json,sys; r=json.load(sys.stdin); print('PASS' if r['pass_for_production'] else 'FAIL', '→ agreement:', r['decision_agreement_rate'])"
```

A sample 8-incident fixture ships at [`eval/incidents_labeled.jsonl`](../eval/incidents_labeled.jsonl) so you can see the harness work before you provide real data.

---

## The honest summary

Three to four weeks of calibration on real incidents with a real human reviewer is the difference between "an AI did the maintenance work" and "an AI **safely** did the maintenance work." There is no shortcut, and Restart OS is designed to refuse to act until the window is observed and the numbers clear.

If your timeline is tight and you are tempted to skip this window, please don't deploy Restart OS — or any agentic maintenance system. The right alternative for that timeline is to keep using Restart OS as a **decision-support tool** in shadow mode permanently: the human still writes the work order, but they spend 5 minutes reviewing the agent's proposal instead of 60 minutes assembling one from scratch. That outcome alone — without ever turning autonomy on — is typically worth $250K-$2M per line per year.

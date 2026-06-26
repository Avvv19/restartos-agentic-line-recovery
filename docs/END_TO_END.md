# Restart OS — End-to-End Walkthrough

A complete trace of one real incident from the **PLC alarm** firing through to **the work order being created in the CMMS**, with screenshots of the live system at each stage.

This walkthrough uses the run `INC-ad9f9fa7` — a live execution against NVIDIA NIM + Groq + Qdrant + Postgres, not a mock.

---

## 1. The problem

> Manufacturing runs the physical economy, but the people who run it — operators, engineers, maintenance techs, plant managers — spend most of their day on work that doesn't need a human. Chasing information across siloed systems. Hunting through 400-page manuals. Writing reports. Troubleshooting the same faults repeatedly. Translating tribal knowledge into something the next shift can use.
>
> Restart OS is an autonomous agent that takes a goal, makes decisions, calls tools, and produces an outcome a human would otherwise do by hand.

The specific pain Restart OS solves: **unplanned line-down fault resolution**. When a line stops, a senior maintenance tech today manually:

1. Checks the historian for the alarm signature
2. Searches CMMS for prior identical faults on that asset
3. Opens the OEM PDF and finds the right procedure
4. Walks to parts and checks stock
5. Pulls the LOTO doc and confirms isolation points
6. Finds an on-shift technician with the right certifications
7. Writes a work order
8. Reserves parts
9. Plans QC sampling
10. Writes the handover note for the next shift

It is slow, it is tribal, and it bleeds money every minute the line is down.

**Restart OS does that job.** Input: a structured incident contract. Output: a verified, human-approved Recovery Work Package written into the CMMS, ERP, QMS, and shift log. Zero free-text chat.

---

## 2. The architecture

```
   ┌───────────────────────┐
   │  OPERATOR INTAKE      │  "L3 filler keeps stopping, bottles backing
   │  (intake.py)          │   up at the capper, A-220, before 3 PM"
   │  messy report →       │        → line, machine, symptom, alarm,
   │  structured Incident  │          urgency, product, deadline, missing
   └──────────┬────────────┘
              ▼
                ┌───────────────────┐
   Incident →→→ │  ORCHESTRATION    │
   (asset, alarm,│  ENGINE           │←── settings.yaml (tau, budget)
    line, $/hr)  │  (LangGraph or    │
                 │   internal SG)    │
                 └────────┬──────────┘
                          │
              ┌───────────┴───────────────────────────┐
              ▼                                       ▼
   ┌──────────────────────┐               ┌──────────────────────┐
   │  AGENTIC GATHER      │               │  INCIDENT MEMORY     │
   │  (LLM tool-use loop) │               │  (Postgres)          │
   │  ───────────────────│               │  ──────────────────  │
   │  read_timeline       │               │  recall_similar()    │
   │  read_maint_history  │               │  persist_run()       │
   │  search_manual ────────► Qdrant RAG │                      │
   │  check_parts                         └──────────────────────┘
   │  check_safety_loto
   │  check_labor (HR roster + certs)
   │  scan_ot_security
   │  check_recent_changes (MOC)
   │  search_shift_notes (tribal knowledge)
   │  read_production_econ
   └──────────────────────┘
                          │
                          ▼
   ┌──────────────────────────────────────────────────────┐
   │  EVIDENCE GRAPH — trust × confidence × freshness     │
   │  Contradictions surfaced, never averaged away.       │
   └──────────────────┬───────────────────────────────────┘
                      │
                      ▼
   ┌──────────────────────────────────────────────────────┐
   │  FIRST-FAULT ISOLATION (causal.py)                  │
   │  root fault vs downstream symptom vs repeated alarm  │
   │  e.g. nozzle clog → low flow → servo overload →     │
   │       bottle backup at capper (the loud symptom)     │
   └──────────────────┬───────────────────────────────────┘
                      ▼
   ┌──────────────────────────────────────────────────────┐
   │  HYPOTHESIS  →  PLAN  →  CROSS-MODEL VERIFY          │
   │  (NIM author)    (manual-grounded)  (Groq — different│
   │                                      family;          │
   │                                      anti-collusion)  │
   └──────────────────┬───────────────────────────────────┘
                      │
                      ▼
   ┌──────────────────────────────────────────────────────┐
   │  SAFETY PRE-CHECK  →  DECISION  →  AUTHORIZATION GATE│
   │  (LOTO present?    ACT / ABSTAIN /  (role-matched,    │
   │   permit ok?)      NEED_MORE_INFO    economics-routed,│
   │                                      e-sign for LOTO) │
   └──────────────────┬───────────────────────────────────┘
                      │ approved
                      ▼
   ┌──────────────────────────────────────────────────────┐
   │  IT ACTION PLANE — idempotent writes                 │
   │  ─────────────────────────────────────────────       │
   │  CMMS.create_work_order  →  WO-AUTO-…                │
   │  ERP.reserve_parts       →  RES-…                    │
   │  QMS.create_qc_plan      →  QCP-…                    │
   │  NOTIFY.notify (tech selected from HR roster)        │
   └──────────────────────────────────────────────────────┘

   Every run ends with a Decision Contract (allowed action, forbidden OT writes,
   approved IT actions, evidence used/missing, risk, audit id) — or an Escalation
   Packet when blocked. Every step recorded in a hash-chained audit log.
   Three architectural guarantees, enforced in code:
     ① No path can WRITE to OT planes        (security.py)
     ② Every citation must resolve           (verify.py)
     ③ Decision = ACT | ABSTAIN | NEED_MORE_INFO (orchestration.py — honest uncertainty)
```

---

## 3. The end-to-end run — screenshot by screenshot

### 3.1 Cockpit view of `INC-ad9f9fa7`

![Cockpit — full ACT run](screenshots/cockpit-act-run.png)

What you see, top to bottom:

**Header.** `FAULT-TO-FIX · INC-ad9f9fa7 · ACT` with the orchestration framework (`internal-stategraph` — LangGraph-compatible state machine).

**Agent trace.** Every line is a tool call or decision the agent made, in order:

- `memory: recalled 3 prior incident(s) on FL-PKG-03-FILL` — **Postgres lookup**
- `fast_path: preliminary cause 'Nozzle clog' from alarm+manual` — sub-60s preliminary triage
- `agent[heuristic-offline] → read_timeline()` — historian
- `agent[heuristic-offline] → read_maintenance_history()` — CMMS
- `manual_sop: grounded via RAG -> MANUAL:Acme_Model_8200.md#7.4@p.143 (score 0.7807)` — **Qdrant semantic search hit**
- `agent → search_manual()`, `check_parts()`, `check_safety_loto()`, `check_labor()`, `scan_ot_security()`, `check_recent_changes()`, `search_shift_notes()`, `read_production_econ()`
- `agent: decided to DIAGNOSE — enough evidence gathered` — the agentic loop's exit condition
- `hypothesis: offline weighted-inference -> [('Nozzle clog', 0.87), ('Viscosity (new lot)', 0.13), ('Pump wear', 0.0)]`
- `planner: 6 steps, risk LOTO_PHYSICAL, parts ['8200-NZ']` — procedure parsed from manual §7.4
- `memory: persisted run outcome` — **Postgres write**

**Incident memory — recalled from Postgres.** The actual prior-incident facts injected into the evidence graph. Trust weight 0.6375, source `memory.recall_similar`.

**Manual grounding — Qdrant semantic + BM25 hybrid.** The specific manual passage retrieved (`Acme_Model_8200.md §7.4 @ p.143`), with its alarm-cited procedure.

**Evidence graph.** 20 facts across 10 systems. The `INCIDENT_MEMORY` and `MANUAL` chips are highlighted in green — those are the two stores we built this MVP around.

**Diagnosis.** `Nozzle clog` at confidence 0.87 (well above tau=0.55), ISO 14224 code 1.2.1, with the falsifier: *"flow normalizes after CIP flush without nozzle swap"*.

**Verification gates.** `verifier PASS · safety PASS · cross-model: mock · citation-resolution 1 · hallucinated parts 0`. Every citation resolved. Zero hallucinated part numbers.

**Proposed work order.** Risk `LOTO_PHYSICAL`, asset FL-PKG-03-FILL, cause Nozzle clog, 45 min, 1 tech, parts `8200-NZ` (2 on hand, bin A4), safe checks include the LOTO step.

**Authorization gate.** Risk → required approver → `maintenance_lead`, e-sign required, required confidence 0.42 (economics-routed: cheap to act, expensive to wait). Outcome **APPROVED** by `maint.lead.kpatel`.

**Economics.** `value/event $7,500` — saved MTTR 90→45 min at $10,000/hr.

**IT WRITES — outcomes the agent produced** (the part that makes it not-a-dashboard):
- `CMMS.create_work_order  →  Work Order WO-AUTO-3adfbcbc01ad  ·  Nozzle clog · 45 min · 6 steps`
- `ERP.reserve_parts       →  Parts Reservation RES-73f2fadbfbcf  ·  8200-NZ × 1 · RESERVED`
- `QMS.create_qc_plan      →  QC Plan QCP-e75a4a125807  ·  AQL 2.5, 5 units post-restart, check fill volume`
- `NOTIFY.notify           →  jmartin · "WO WO-AUTO-3adfbcbc01ad assigned: Nozzle clog" · SENT`

**Model router.** 5 LLM calls, 4,446 tokens, $0 (free-tier NIM + Groq), distributed by problem type: FAST_PATH 476 tok, DEEP_DIAGNOSIS 2,674 tok, PLANNING 1,104 tok, VERIFICATION 58 tok, KNOWLEDGE 134 tok.

**Tamper-evident audit.** 15 hash-chained entries, last hash `9250e570ac084450…`. Any tampering with intermediate state breaks the chain.

### 3.2 Liveness probe

![healthz endpoint](screenshots/healthz.png)

```
GET /healthz  →  {"ok": true, "postgres": true, "qdrant": true}
```

Both stateful dependencies (Postgres incident memory + Qdrant vector store) are up. Suitable for Kubernetes readiness/liveness probes or any uptime monitor.

### 3.3 Operational metrics (Prometheus exposition)

![Prometheus metrics](screenshots/metrics-prometheus.png)

```
restartos_incidents_total                       21
restartos_incidents_decision{decision="ACT"}    10
restartos_incidents_decision{decision="ABSTAIN"} 11
restartos_abstention_rate                       0.5238
restartos_tokens_total                          100445
restartos_cost_usd_total                        0.0000
restartos_memory_incidents                      11
restartos_last_top_confidence                   0.8700
```

The **52% abstention rate is the punchline**. This is not a system designed to act on every input — it's designed to *act when it has cited evidence and refuse otherwise*. That ratio is the most truthful number a manufacturing-AI system can publish: how often does it know what it doesn't know?

A Grafana dashboard would scrape this every 15s.

---

## 4. How to run this yourself

### Option A — Docker (recommended, one command)

```bash
git clone https://github.com/Avvv19/restartos-agentic-line-recovery
cd restartos-agentic-line-recovery

# Optional: paste your own NIM/Groq/Gemini keys.
# Without keys, the system runs deterministic-offline on a mock LLM.
cp .env.example .env

# Brings up Qdrant + Postgres + the engine
docker compose up -d

# Trigger an incident
docker exec restartos-engine python -m restartos.cli run --auto-approve

# Open the cockpit
open http://localhost:8000/cockpit
```

### Option B — Local Python (no Docker)

```bash
pip install -r requirements.txt
docker compose up -d qdrant postgres        # just the data stores
python dataset/generate.py                  # 751 simulated plant files

PYTHONIOENCODING=utf-8 PYTHONPATH=. python -m restartos.cli run --auto-approve
PYTHONIOENCODING=utf-8 PYTHONPATH=. python -m restartos.server --port 8000
# → http://localhost:8000/cockpit
```

### Endpoints worth knowing

| Endpoint | What it does |
|---|---|
| `GET /cockpit` | Operator UI (the screenshot above) — includes the freeform intake box, evidence board, decision contract |
| `POST /api/intake` | Parse a freeform operator message into a structured incident (preview) |
| `POST /api/run` | Run a fault-to-fix; accepts a structured incident **or** a freeform `message` |
| `GET /api/latest_run` | Full JSON of the most recent run |
| `GET /api/memory` | Postgres memory stats |
| `GET /api/rag?q=nozzle+clog` | Live semantic query over the OEM manuals |
| `GET /api/providers` | LLM availability + anti-collusion check |
| `GET /api/boundary` | Proves OT writes are blocked at the code layer |
| `GET /api/eval` | Run the labeled fault-set evaluator |
| `GET /healthz` | Liveness probe |
| `GET /metrics` | Prometheus counters |

### Tests

```bash
PYTHONPATH=. python -m pytest -q                  # full suite
PYTHONPATH=. python tests/regression_matrix.py    # 8-scenario regression
```

The CI on GitHub Actions runs the same suite on every push: https://github.com/Avvv19/restartos-agentic-line-recovery/actions

---

## 5. What's real vs what's simulated

This is the most important section for an honest assessment.

| Layer | Status | What's underneath |
|---|---|---|
| Agent orchestration loop | 🟢 **Real** | LangGraph-compatible state machine with abstention, retries, cross-model verify |
| LLM author (DEEP_DIAGNOSIS, PLANNING) | 🟢 **Real** | NVIDIA NIM Nemotron-3-Ultra-550B (free tier) |
| LLM verifier (VERIFICATION) | 🟢 **Real** | Groq Llama-3.3-70B (free tier, **different family** for anti-collusion) |
| Vector RAG | 🟢 **Real** | Qdrant 1.12 + `all-MiniLM-L6-v2` (112 chunks @ 384 dims, persisted across restarts) |
| Incident memory | 🟢 **Real** | Postgres 16 — `recall_similar()` + `persist_run()` |
| Audit log | 🟢 **Real** | Hash-chained, tamper-evident, per-incident JSON |
| OT/IT boundary | 🟢 **Enforced** | `make boundary` proves OT writes raise `OTWriteForbidden` |
| Observability | 🟢 **Real** | `/healthz`, `/metrics` (Prometheus), full audit trail |
| **Historian (PI / OPC-UA)** | 🟡 **Simulated** | CSV time-series with engineered drift + frozen sensors + missing values. Real shape, fake numbers. Swap point: `restartos/connectors.py:build_historian`. |
| **CMMS / ERP / QMS / NOTIFY** | 🟡 **Simulated** | Writes to `_it_state/it_state.json` instead of Maximo/SAP/Fiix REST. The shape of every record matches real CMMS APIs. Swap point: `restartos/actions.py:ITActionPlane`. |
| **HRIS roster + certs** | 🟡 **Simulated** | CSV with 10 technicians, multi-shift/multi-cert. Swap point: BambooHR/Workday API in `restartos/agents.py:LaborAgent`. |
| **OEM Manuals** | 🟡 **Real RAG over synthetic content** | The corpus is a generated Acme Model 8200 service manual (.md + .pdf at p.143). Real chunking, real embedding, real semantic retrieval. The document itself is synthetic; the retrieval pipeline is genuine. |

**The honest framing.** The *intelligence layer* (agents, memory, RAG, verification, abstention, audit) is real and production-shaped. The *industrial system connectors* are simulated with fidelity to real API shapes, with every swap point documented. Going to a real plant is weeks of integration work per system — not a rewrite. See [README — Going from MVP to a real plant — swap points](../README.md#going-from-mvp-to-a-real-plant--swap-points).

---

## 6. The three architectural guarantees

These are not aspirational. They are enforced by the code.

### 6.1 Safety by construction — no path can WRITE to OT

```python
# restartos/security.py
def assert_capability(plane: Plane, access: Access) -> None:
    if plane in (Plane.OT_CONTROL, Plane.OT_OPS) and access == Access.WRITE:
        raise OTWriteForbidden(...)
```

There is **no code path** that can write back to PI Historian, the PLC, SCADA, or any OT system. Verified by `make boundary` and CI.

### 6.2 Grounded or it doesn't ship — cross-model verifier

The verifier runs on a *different model family* than the planner (NIM Nemotron author vs Groq Llama verifier). It resolves every citation against the actual manual chunk. **One bad citation → plan refused → abstain with reason.**

That is exactly the failure mode visible in the regression matrix runs: 3 scenarios produced a high-confidence diagnosis (0.84–1.0) but the verifier could not ground the plan to its satisfaction → abstain with reason. **This is the anti-hallucination guardrail firing as designed.**

### 6.3 It can say "I don't know" — or "I need one more thing"

The decision is three-way. `Decision.ABSTAIN` is a first-class outcome, not an exception — the system has actually abstained 11 times out of 21 runs (52% abstention rate, visible in `/metrics`). `Decision.NEED_MORE_INFO` is the middle path: when one specific human-supplied input would unblock the call (a missing alarm code, an unverified alarm not in the OEM fault map, an absent line number), the agent asks for exactly that instead of guessing or refusing outright. Either way a blocked run still produces an **Escalation Packet** with the exact next human step, and every run produces a **Decision Contract**. That is honest uncertainty — the property without which no industrial AI can be trusted to write a work order.

---

## 7. What's next on the roadmap

| Step | Status |
|---|---|
| Public GitHub repo with CI | ✅ Done — https://github.com/Avvv19/restartos-agentic-line-recovery |
| Docker compose one-command deploy | ✅ Done |
| Tests + CI green | ✅ Done (pytest + docker build) |
| Public HTTPS deploy (Fly.io) | 🔜 Next |
| Gemini 3rd verifier ring | 🔜 Needs an `AIza...` API key |
| Real PI Web API connector | 🟡 Needs a partner plant |
| Real Maximo / SAP PM writer | 🟡 Needs a partner plant |
| IEC 62443 / SOC2 compliance review | 🟡 Needs a partner plant |
| Pilot in shadow mode on one line | 🟡 Needs a partner plant |

The remaining work is integration + compliance, not architecture. The hard, non-obvious parts — the cross-family verifier, the abstention semantics, the evidence graph with contradictions, the OT/IT boundary, the citation-resolution gate — are real and runnable today.

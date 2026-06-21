# RestartOS — Agentic Line-Recovery for Manufacturing

> Problem Statement 9 · Agent Orchestration. An agent that takes a *line-down goal*,
> reasons across every plant silo, and produces a **verified Recovery Work Package** —
> the outcome a senior maintenance tech assembles by hand today.
> **Not a chatbot. Not a dashboard. An agent.**

**📖 [End-to-End Walkthrough with screenshots](docs/END_TO_END.md)** — start here if you want to see exactly what the system does in one real run, panel by panel.

```
Goal in   →  "Line 3 filler down, alarm A-220 — get it running safely, fast."
Outcome out →  grounded diagnosis + safety-checked procedure + human-approved
               work order in the CMMS + the fix captured for the next shift.
```

This repository is the working, production-shaped implementation of the
[RestartOS v2 architecture](../restartos-architecture.html). Every plane in that
diagram is a real module here, and the whole fault-to-fix flow runs end-to-end
**offline with zero API keys** (a deterministic mock model), then swaps in real
Claude / GPT / local Ollama models when keys are present.

---

## Status — code-production ready

[![CI](https://github.com/Avvv19/restartos-agentic-line-recovery/actions/workflows/ci.yml/badge.svg)](https://github.com/Avvv19/restartos-agentic-line-recovery/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](#license)

| Layer | Status | Evidence |
|---|---|---|
| 4-agent orchestration loop | 🟢 **Real** | Live tool-calling, abstention, retries, cross-model verifier |
| LLM providers | 🟢 **Real** | NVIDIA NIM + Groq Llama on free tiers (anti-collusion across families) |
| Vector RAG over OEM manuals | 🟢 **Real** | Qdrant + `all-MiniLM-L6-v2` (112 chunks @ 384 dims, persistent) |
| Persistent incident memory | 🟢 **Real** | Postgres 16 — second run on same fault recalls prior outcomes |
| OT/IT boundary | 🟢 **Enforced** | `make boundary` proves OT writes raise `OTWriteForbidden` |
| Audit log | 🟢 **Real** | Hash-chained, tamper-evident, per-incident JSON |
| Industrial source connectors (PI, Maximo, SAP) | 🟡 **Simulated** | CSV/JSON read & write at correct API shape; documented swap points |
| `/metrics`, `/healthz`, CI gates | 🟢 **Real** | Prometheus exposition, pytest in CI on every push |

> **Honest framing.** This is a **working MVP with simulated industrial sources**, not a production deployment on a live plant. The intelligence layer (agents, memory, RAG, verification, abstention, audit) is real and runnable. The OT/IT system connectors are simulated with fidelity. Swap points for each are documented at the bottom of this README.

---

## Quick start — Docker (one command)

```bash
# Optional: provide LLM API keys (system runs deterministic-offline without them)
cp .env.example .env && $EDITOR .env

# Bring up Qdrant + Postgres + the RestartOS engine
docker compose up -d

# Trigger an incident end-to-end
docker exec restartos-engine python -m restartos.cli run --auto-approve

# Open the operator cockpit
open http://localhost:8000/cockpit
```

Endpoints once running:

| Endpoint | What you get |
|---|---|
| `GET /cockpit` | Operator cockpit UI — renders latest run with memory + Qdrant grounding + IT writes |
| `GET /api/latest_run` | Full JSON of the most recent run (audit trail, evidence, gate, IT actions) |
| `GET /api/memory` | Postgres incident-memory stats |
| `GET /api/providers` | Live LLM provider availability + anti-collusion check |
| `GET /api/rag?q=...` | Run a semantic query against the manual corpus |
| `GET /api/boundary` | Prove OT write paths are blocked |
| `GET /api/eval` | Run the labeled fault-set evaluator |
| `GET /healthz` | Liveness probe (Postgres + Qdrant up) |
| `GET /metrics` | Prometheus-format counters (incidents, abstention rate, tokens, cost) |

## Quick start — local Python (no Docker)

```bash
pip install -r requirements.txt
python3 dataset/generate.py              # writes 750+ messy multi-format files -> ./_data
PYTHONPATH=. python3 -m restartos.cli run --auto-approve     # full fault-to-fix
PYTHONPATH=. python3 -m restartos.cli eval                   # accuracy / safety metrics
PYTHONPATH=. python3 -m restartos.cli boundary-test          # prove no OT write path
PYTHONPATH=. python3 -m pytest -q                            # full test suite
```

`make data run eval boundary test` wraps the same commands.

---

## What it does, in one run

1. **Intake** — a line-down `Incident` contract (asset, symptom, alarm, $/hr), not a question.
2. **Resolve asset** — the keystone join: `"Line 3 filler"` → `FL-PKG-03-FILL`,
   PI tags `4471/4472/4473`, OEM `Model 8200`. Tolerates the messy registry
   (`FL-PKG-3-FILL` vs `FL-PKG-03-FILL`). If it can't confidently resolve → **abstain**.
3. **Fast path** — preliminary likely-cause in one hop from alarm + manual.
4. **Gather (specialist lenses)** — timeline, maintenance/MTBF, manual/SOP, safety,
   change/MOC, parts, production/econ, labor, shift-notes (tribal slang), OT-security.
   Each writes **timestamped, cited, trust-weighted evidence** into the Evidence Graph.
5. **Diagnose** — differential, evidence-weighted, *calibrated* confidence, with an
   explicit "what would change my mind". Bounded inner loop re-gathers if needed.
6. **Plan** — grounded recovery procedure parsed from the manual, parts resolved
   against inventory, labor + LOTO classified.
7. **Cross-model verify** — a **different model family** tries to *refute* the plan.
   Every citation must *resolve* to a real passage; every part must exist. (The demo
   deliberately injects a hallucinated `p.212` citation on the first pass — the
   verifier catches it and the engine re-plans. Watch the trace.)
8. **Safety pre-check** — LOTO present? contradicts the manual safety section? permit?
9. **Authorization gate** — role-matched + **economics-routed** (a $50k/hr line acts
   on lower confidence than a $500/hr line); e-signature where required.
10. **Act** — *idempotent* writes to IT only: CMMS work order, parts reservation,
    QMS sampling plan, notifications.
11. **Monitor + learn** — confirm the fix, capture it as a validated known-fix.

Everything is recorded in a **tamper-evident, hash-chained audit**.

---

## The three non-negotiables (enforced in code)

| Principle | Where it lives | How it's enforced |
|---|---|---|
| **Safety by construction** — no path to actuate the line | `security.py` | The capability matrix makes every OT plane READ-only. `assert_capability(OT, WRITE)` raises `OTWriteForbidden`. Run `boundary-test`. |
| **Grounded or it doesn't ship** | `verify.py` | Cross-model verifier resolves *every* citation against the real manual; unresolved → blocked. Citation-resolution target **100%**, hallucinated-part target **0**. |
| **It can say "I don't know"** | `orchestration.py` | Abstain is a first-class `Decision`: low confidence or unresolved contradiction → escalate to OEM/senior. Measured by **abstention precision** in evals. |

---

## Architecture → code map

| Architecture plane | Module |
|---|---|
| ① Intake / Incident contract | `restartos/domain.py` (`Incident`) |
| ② Orchestration Engine (plan·delegate·retry·abstain) | `restartos/orchestration.py` |
| ③ OT Data Plane (READ-ONLY, Purdue L0–L3) | `restartos/data/adapters.py` (Historian, MES) |
| ④ Agent Reasoning Plane (specialist lenses) | `restartos/agents.py` |
| ⑤ Shared Evidence Graph (trust + freshness + contradictions) | `restartos/evidence.py` |
| ⑥ Decision → Verify → Safety → Gate | `restartos/verify.py`, `restartos/gate.py` |
| ⑦ Recovery Work Package (the outcome) | `restartos/domain.py` (`WorkPackage`) |
| ⑧ IT Action Plane (WRITE, L4, idempotent) | `restartos/actions.py` |
| Tamper-evident audit (spans every layer) | `restartos/audit.py` |
| Anti-hallucination wall | `restartos/verify.py` (`CrossModelVerifier`) |
| Eval & trust plane | `restartos/evals.py` |
| Surface (verify reasoning + go/no-go, **no KPI charts**) | `ui/cockpit.html` |

---

## Orchestration framework (the hybrid)

The macro-flow is expressed as a **state graph**
(`scope → resolve → fast-path → gather∥ → diagnose⇄ → plan → verify → safety →
gate → act → monitor → learn`). When `langgraph` is installed it compiles as a
real LangGraph `StateGraph`; otherwise an **internal executor runs the identical
node functions and edges** so the demo never depends on an install. The specialist
lenses are **CrewAI-style single-role agents** (`SPECIALIST_LENSES` in `agents.py`).
`RunResult.framework` reports which path executed.

## Multi-provider model router (`restartos/llm/`)

Token usage is **maximized by problem type** (`config/model_routing.yaml`):

| Problem type | Preferred tier | Budget |
|---|---|---|
| `FAST_PATH` / `EVIDENCE_LENS` / `KNOWLEDGE` | local Ollama → Haiku → mock | small |
| `DEEP_DIAGNOSIS` | Opus → GPT-4o → mock | large |
| `PLANNING` | Sonnet → GPT-4o → mock | medium |
| `VERIFICATION` | **different family** (GPT-4o → Sonnet → mock) | large |

The router tracks tokens, cost and latency per call, enforces a per-incident
budget (count / cost / wall-clock) that drives the bounded retry loop, and
guarantees the verifier never lands on the author's exact provider.

---

## The dataset (`dataset/generate.py` → `_data/`)

A deliberately **messy, 750+ file, 7-format** plant export — because a clean
dataset would make the demo a lie. It includes seeded, documented defects:

- cross-silo key mismatch (`FL-PKG-3-FILL` vs `FL-PKG-03-FILL`, `Acme 8200` vs `Acme Model 8200`)
- sensor **drift** (vibration) and a **frozen/stale** sensor (cap torque)
- ~1% missing/`Bad`-quality historian rows
- a **duplicate work order with contradictory cause** (`WO-44999`)
- a part referenced everywhere but **0 on hand** (`8200-SL`, 14-day lead)
- an **open-risk MOC** (new high-viscosity lot — a real alternative root cause)
- **tribal-knowledge shift notes** in slang (`"chucked a wobbly"`, `"vibbo"`, `"manky"`)
- a planted **security defect**: hardcoded credential + `verify_ssl=false` (flagged by the OT-security lens)

`_data/_manifest.json` is the index + intentional-defect ledger used by the evals.

Formats: `.csv .json .xml .xlsx .pdf .md .ini`.

---

## Measured results (offline mock run)

```
diagnosis_top1_accuracy : 1.0
citation_resolution_rate: 1.0      (target 100%)
hallucinated_part_rate  : 0        (target 0)
safety_violation_rate   : 0        (target 0)
abstention_precision    : 1.0
7/7 pipeline + safety tests pass
audit chain: intact (tamper detected at the mutated entry)
```

With real keys, swap the providers in `.env`; the same grounded context drives
the same structured outcome — the **data does the grounding**, not the model's priors.

---

## How this project would die (and how this build avoids it)

It drifts into RAG-over-manuals (no outcome) · skips the asset registry (can't join) ·
verifier shares the author's model (collusion) · no abstention (confident wrong guess) ·
any write path toward control (safety review ends it) · no eval set · KPI charts in the UI.
Each is countered above — see the kill-list in the architecture and the test suite.

---

## v2.1 — production hardening (now real, verified)

These four were upgraded from "pluggable" to **built and tested**:

**1. Real LangGraph macro-graph** (`restartos/lg.py`). The flow compiles as an
actual `langgraph` `StateGraph` with explicit conditional edges for the abstain
branch and the verify→replan loop. The engine delegates to it automatically when
`langgraph` is installed (`RunResult.framework == "langgraph"`), and falls back to
the identical-behavior internal executor otherwise (`RESTARTOS_FORCE_INTERNAL=1`
to force it). Specialist lenses run as a **CrewAI** crew when `crewai` is installed
(guarded by import; `build_specialist_crew`).

**2. Semantic manual RAG** (`restartos/rag.py`). A real retriever over markdown
**and PDF** manuals: pure-Python **BM25** by default (zero deps, deterministic,
offline), with an optional dense lane (sentence-transformers locally, or OpenAI
embeddings when `OPENAI_API_KEY` is set) fused with BM25. `ManualAgent` now grounds
on natural-language alarms over arbitrary OEM docs; the strict exact-citation
verifier is unchanged. Try it: `python -m restartos.cli rag "clear a clogged nozzle with lockout"`.

**3. Real model calls** (`restartos/llm/`). Anthropic, OpenAI and Ollama providers
are wired with cost accounting; the router routes by problem type and **forces the
verifier onto a different model family than the author** (anti-collusion — unit
tested). Smoke-test your setup: `python -m restartos.cli providers`.

**4. Real REST connectors** (`restartos/connectors.py`). PI Web API-style
historian (OT, **read-only — no write method exists**) and Maximo/SAP-style CMMS
(IT, gated idempotent POST), with bearer auth, bounded retries + backoff, client
rate-limiting and timeouts — stdlib only. Switch from dataset to live with
`RESTARTOS_LIVE=1` + `PI_BASE_URL` / `CMMS_BASE_URL` (+ token env vars). Verified
against a local mock HTTP server in the test suite.

### Commands added
```
python -m restartos.cli providers     # provider availability + anti-collusion routing + smoke call
python -m restartos.cli rag "<query>" # semantic retrieval over manuals (md + pdf)
```

### Test suite
`PYTHONPATH=. python3 tests/test_pipeline.py` → **10/10**: happy-path diagnosis,
verifier catch+replan, abstain, OT-write-forbidden, idempotency, tamper-evident
audit, economics-routed gate rejection, real REST connectors (live HTTP),
anti-collusion routing, and RAG retrieval/grounding.

### To go fully live
1. `pip install -r requirements.txt`
2. Put `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` in `.env` (different families = real anti-collusion).
3. Set `RESTARTOS_LIVE=1` and your `PI_BASE_URL` / `CMMS_BASE_URL` + token env vars.
4. Drop your real OEM manual PDFs into `_data/manuals/` — the RAG indexes them on next run.

---

## v2.2 — the live presenter UI (for the video walkthrough)

### Just want to SEE it work? Two ways

**A. Zero setup (double-click).** Open `ui/app_standalone.html` in any browser — it
replays the **real engine output** (captured from an actual run) through the full
step-by-step UI. No Python, no server. Great for a quick look or a screen recording.

**B. Live engine (one click).** Double-click `start_windows.bat` (Windows) or
`start_macos.command` (Mac). It installs the one dependency, generates the dataset on
first run, starts the engine, and opens `http://localhost:8000`. Every click now hits
the real running engine. This is the version where you change inputs and it recomputes.


A real local web app that runs the engine in-process and walks every stage of the
fault-to-fix flow, with a plain-English explanation on each — built to be recorded.

```bash
PYTHONPATH=. python3 -m restartos.server      # or: make serve
# open http://localhost:8000
```

`restartos/server.py` is **stdlib-only** (no new dependencies). It serves
`ui/app.html` and live JSON endpoints: `POST /api/run`, `GET /api/eval`,
`/api/providers`, `/api/rag?q=`, `/api/boundary`, `/api/dataset`.

### The UI shows, stage by stage
Goal in → asset join → fast path → **every cited fact from every silo** (the whole
organisation's knowledge, trust-weighted) → differential diagnosis → grounded plan →
**cross-model verifier catching the bad citation and re-planning** → safety pre-check →
role-matched economics-routed gate → idempotent IT writes → monitor + learn + audit.
A left-rail stepper plus **Next / Play** controls reveal one stage at a time so you can
narrate. A "Governance & proofs" panel runs the evals, the OT/IT boundary test, the
model-routing check, the dataset manifest and a live RAG query — each hitting the
running engine.

### Suggested 3-minute recording script
1. **Open** `http://localhost:8000` — read the goal line on camera.
2. Click **▶ Run fault-to-fix** — point out the green banner: `ACT · framework langgraph · Nozzle clog @ 0.87`.
3. Hit **▷ Play** and narrate each stage as it reveals:
   - *Resolve* — "it joins PI tag ↔ SAP funcloc ↔ Model 8200 across silos that disagree."
   - *Gather* — "19 cited facts from 9 silos, each trust-weighted — this is the whole plant's knowledge joined."
   - *Diagnose* — "ranked differential, calibrated confidence, and what would change its mind."
   - *Verify* — "a different model refutes it, catches a citation to a page that doesn't exist, and forces a re-plan."
   - *Gate* — "role-matched, economics-routed, e-signature for LOTO."
   - *Act* — "idempotent writes to IT only."
4. Scroll to **Governance & proofs**: click **OT/IT boundary test** ("it physically cannot write to control"), **Run evals** (100% citation resolution, 0 hallucinated parts), **Model routing** (author vs verifier on different families).
5. Change the asset to `ghost xyz` / line `Line 9`, run again — show it **ABSTAIN and escalate** instead of guessing.

---

## v2.3 — the engine actually reasons (model-driven decisions)

The diagnosis is now produced by the model, not by decorative scoring. `HypothesisAgent`
builds a real prompt from the full evidence graph, calls the routed model, parses the
model's STRICT-JSON differential, and **uses that as the decision** (cause, calibrated
confidence, supporting evidence ids, falsifier). `RunResult.reasoning_engine` reports who
decided:

- **With a key** → e.g. `anthropic:claude-opus-4-8`. The work order's confidence is the
  model's number. Verified by `test_diagnosis_is_model_driven_when_a_model_is_present`
  (stub model returns 0.79 → the engine acts on 0.79, not the heuristic's 0.87).
- **Offline** → `offline-deterministic-inference`: a real evidence-weighted inference over
  the live dataset (trust × confidence × freshness-decay), not a replay.

What is genuinely real either way: the tool calls (data adapters + REST connectors), the
evidence graph, the cross-model verifier's hard groundedness check, the safety pre-check,
the role-matched gate, the idempotent IT writes, and the hash-chained audit.

### Run the true live LLM engine
```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
echo "OPENAI_API_KEY=sk-..."        >> .env   # different family => real anti-collusion verify
make serve                                     # http://localhost:8000
```
Each run now calls real Claude/GPT; the UI shows `reasoned by anthropic:claude-opus-4-8`.

---

## v2.4 — a real agentic tool-use loop (not a fixed pipeline)

The supervisor no longer runs every lens in a hardcoded order. It now DECIDES which
tool to call next, one at a time, and when it has enough, it diagnoses.

- `restartos/tools.py` — a typed, permissioned **toolbelt** over the plant systems
  (read_timeline, read_maintenance_history, search_manual, check_recent_changes,
  check_parts, check_safety_loto, read_production_econ, check_labor,
  search_shift_notes, scan_ot_security). Read-only against OT by construction.
- `restartos/agent_loop.py` — a ReAct-style loop with two drivers:
  - **LLM tool-use** (`tool_policy: llm-tooluse`): with a key, the model returns a
    STRICT-JSON action `{action,args,rationale}` each turn — provider-agnostic
    (Claude/GPT/Ollama). Proven by `test_agentic_loop_model_chooses_the_tool_sequence`
    (the model calls exactly `read_timeline, search_manual` then diagnoses — not all 10).
  - **Adaptive offline policy** (`tool_policy: heuristic-offline`): reacts to live
    evidence (once a clog signature appears it prioritises manual → parts → safety).
    Not a replay.

Every decision and its rationale is in `RunResult.trace` and shown in the UI's
"Agentic tool-use" stage. `RunResult.tool_policy` and `tools_called` report what happened.

The full chain is now agentic end to end: **the model chooses the tools**, **the model
reasons** the diagnosis, **a different model refutes** the plan, deterministic safety +
groundedness gates hold the line, a human approves, and the outcome is written to IT and
audited. 14/14 tests pass.

---

## v2.5 — live model providers wired (NVIDIA NIM · Groq · Gemini)

Keys live ONLY in `.env` (never in source; `.env` is git-ignored). The router
loads them automatically and routes for anti-collusion:

- **Author** (deep diagnosis + planning) → **NVIDIA NIM** (`nvidia/nemotron-3-ultra-550b-a55b`)
- **Verifier** (refute the plan) → **Groq** (`llama-3.3-70b-versatile`) — a different family
- **Fast-path / knowledge** → **Gemini** (`gemini-2.0-flash`)

Run it live (on a machine with network access to those endpoints):
```bash
python -m restartos.cli providers     # smoke-test: shows live providers + author/verifier split + a real call
make serve                            # http://localhost:8000 — runs now use NIM/Groq/Gemini
```
When live, the UI shows `reasoned by nim/nvidia/nemotron-3-ultra-550b-a55b` and the
verifier runs on Groq. If a model id 404s for your account, change `*_MODEL` in `.env`.
The engine **degrades gracefully** to the offline deterministic inference if a provider
is unreachable, so a demo never hard-fails.

SECURITY: the keys you pasted were shared in plaintext — **rotate/revoke them** after
testing. RestartOS's own security lens flags hardcoded credentials for exactly this reason.

---

## v2.6 — Downtime Triage Agent workbench (operational control room)

`ui/workbench.html` — a React + Tailwind + Lucide control-room UI (central `useReducer`
state modelling the full tool trace). It is **not** a slideshow: it computes live.

- **Backend mode** — `make serve` then open `http://localhost:8000/` : every run hits the
  Python engine via `POST /api/run` (real models when your keys work). The walkthrough UI
  is at `/walkthrough`.
- **Standalone mode** — double-click `ui/workbench.html` : the embedded real JS engine
  computes from the live KB (no server, no key). It auto-tries the backend first, falls
  back to the in-browser engine.

Interactivity that proves it's real:
- **State-driven timeline** from the engine's actual tool calls (real per-tool timing +
  evidence counts); the in-progress step shows a live skeleton.
- **Evidence viewer** — click any citation chip to open a modal showing the *actual* source
  the agent read (manual §7.4 text, historian trend numbers, CMMS recurring, MOC record,
  shift note, parts row) — not a placeholder.
- **Editable CMMS + Shift Handoff** bound to reactive state (export updates as you type).
- **Command bar** — type `rate 50000`, `line 2 filler`, `alarm A-220`, `unknown asset`, or
  `re-evaluate`; the agent **re-runs** and the whole packet recomputes. Try `unknown asset`
  to watch it abstain and escalate instead of guessing.

---

## Going from MVP to a real plant — swap points

Every simulated connector has a documented swap location. Replacing the simulator with a real client is the unit of integration work per system.

| Simulator | File | Real connector | What changes |
|---|---|---|---|
| Historian (PI / OPC-UA) | `restartos/connectors.py:build_historian` | PI Web API or `asyncua` client | Read-only HTTP/WS client; trends + tag values |
| CMMS work order writer | `restartos/actions.py:ITActionPlane.create_work_order` | Maximo / Fiix / SAP PM REST | OAuth2 + idempotency-key headers |
| ERP parts reservation | `restartos/actions.py:ITActionPlane.reserve_parts` | SAP S/4 OData or NetSuite REST | Reservation API with material movement |
| HRIS roster + certs | `restartos/data/adapters.py:HRIS_ROSTER` | BambooHR / Workday API | Daily-cached read |
| Shift notes / tribal knowledge | `restartos/data/adapters.py:ShiftNotesAdapter` | Slack Events API or Teams webhook | Listener that ingests into Postgres |
| OEM manuals | `_data/manuals/*.md` | OEM portal PDFs ingested via `dataset/generate.py` extension | One-time embed into Qdrant |

### Plant-pilot checklist (the *real* production path)

1. **Network architecture** — engine runs in IT DMZ. OT-side collector is a separate read-only process pushing tags into Kafka. IT/OT segmentation reviewed by the plant's architect.
2. **Service accounts** — read-only PI / Maximo creds, scoped to one line, in Vault / AWS Secrets Manager (not `.env`).
3. **Eval calibration** — collect 50-100 historically resolved incidents on this asset, label ground truth, measure top-1 root cause accuracy and abstention precision. **Do not deploy until >85% on this set.**
4. **Shadow mode (4-8 weeks)** — agent emits work orders but they go to a queue, not the CMMS. Human reviewer scores each against what they would have done. Track agreement rate.
5. **Gate handoff** — only after sustained >85% agreement do you let the agent's gate dispatch real work orders, and only for the lowest-risk class (`WORK_ORDER_DRAFT`).
6. **Compliance** — IEC 62443 risk assessment, SOC2 Type II if SaaS, ISO 27001, NERC CIP if regulated utility.
7. **24/7 ops** — PagerDuty rotation, SLO of <5min MTTR for the agent service, runbooks.

The bottleneck is **not** the code — it's plant approval cycles, measured in months, not weeks. The system you see here is the part that takes those months to build correctly; the connectors are weeks of plumbing on top.

---

## License

MIT — see [LICENSE](LICENSE). No warranty for industrial deployment without the calibration and compliance steps above.

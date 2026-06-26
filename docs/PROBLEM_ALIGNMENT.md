# 🧭 Problem Alignment — does this actually solve the right problem?

> This page maps Restart OS, point by point, to the manufacturing **agent-orchestration** brief: the real pains it removes, and the hard requirements it has to meet (it must be an *agent* — not a chatbot, not a dashboard). Each row links to the exact place in the running code.

---

## 😣 The pains it removes

The brief describes a maintenance tech losing 45–90 minutes of *non-repair* work every time a line goes down. Here's each pain and how Restart OS takes it off their plate.

| The pain (in human terms) | How Restart OS removes it | Where in the code |
|---|---|---|
| **Chasing information across siloed systems** — historian, CMMS, ERP, HR, safety, MES, shift notes | One agentic loop calls all of them as typed tools and merges the answers into a single evidence board | `agent_loop.py`, `tools.py`, `evidence.py` |
| **Hunting through 400-page manuals** | Semantic search (RAG) returns the exact section and *verifies* the citation resolves to a real passage | `rag.py`, `verify.py` |
| **Writing reports & work orders by hand** | The Recovery Work Package is generated complete — work order, parts, QC plan, handoff | `domain.py` → `WorkPackage` |
| **Troubleshooting the same fault again and again** | It mines the maintenance history for repeat-failure patterns and flags symptom-only fixes | `data/adapters.py` → `CMMSAdapter.patterns()` |
| **Tribal knowledge that never reaches the next shift** | It lifts know-how out of free-text shift notes, marks it *unverified*, and routes it for a lead to confirm | `agents.py` → `ShiftNotesAgent` |
| **The next shift starting blind** | A structured handoff: what was checked, what to monitor, what's reserved-but-not-installed, LOTO status | `orchestration.py` → `_build_handoff` |
| **Chasing the loudest alarm instead of the real fault** | First-fault isolation separates the root cause from the downstream noise | `causal.py` |

---

## ✅ The hard requirements — it must be an *agent*

The brief is strict: this has to be an autonomous agent that takes a goal, makes decisions, calls tools, and produces an outcome. Not a chatbot. Not a dashboard.

| Requirement | How Restart OS satisfies it | Proof you can run |
|---|---|---|
| **Takes a goal** (not a question) | A freeform operator report is parsed into a structured incident, *or* a structured `Incident` is passed directly | `intake.py`; Demo Scene 1 |
| **Makes decisions** | Chooses which tool to call next; ranks the differential; isolates first fault; decides **ACT / NEED_MORE_INFO / ABSTAIN**; routes the gate | `agent_loop.py`, `orchestration.py`; Scenes 1–3 |
| **Calls tools** | A typed toolbelt over real plant systems — historian, CMMS, manual RAG, parts, safety, HR, MES, MOC, shift notes, security | `tools.py`; the toolbelt panel in the demo |
| **Produces an outcome** | Idempotent IT-side writes (work order, parts reservation, QC plan, notification) — the artifact a human assembled by hand | `actions.py`; Scene 1 "IT writes" |
| **Knows its limits** | Refuses or asks when evidence is weak or missing — and explains why | `contracts.py`; Scenes 2 & 3 |
| **Stays safe** | Cannot write to OT by construction; a human approves every IT write | `security.py`; Scene 5 |

---

## 🚫 Why it is **not a chatbot**

A chatbot answers questions with text. Restart OS doesn't chat — it **does the job**. Goal in → a real, structured Recovery Work Package out, plus actual writes to business systems after approval. There is no "conversation"; there is an incident, a decision, and an outcome.

## 🚫 Why it is **not a dashboard**

A dashboard *shows you data and leaves the thinking to you*. Restart OS does the opposite:

- a dashboard says *"head pressure is high"*; Restart OS says *"the nozzle is clogged, here's the cited procedure, the part, the tech, and the work order — approve?"*
- a dashboard shows ten alarms; Restart OS tells you **which one started it**;
- a dashboard never refuses; Restart OS **abstains** when it can't ground a safe answer;
- a dashboard never acts; Restart OS **writes the work order** (after a human says yes).

See [the "Why this is not a dashboard" section in the README](../README.md#-why-this-is-not-a-dashboard) and [SAFETY_MODEL.md](SAFETY_MODEL.md).

---

## 🧪 See every claim proven

Don't take our word for it — each property is a one-click demo scene or a runnable command:

```bash
python -m restartos.cli demo            # the five scenes
python -m restartos.cli boundary-test   # prove OT writes are blocked
python tests/regression_matrix.py       # 8 end-to-end scenarios, CI-gated
```

| Property | Where it's proven |
|---|---|
| Acts on a messy goal | Demo Scene 1 |
| Asks for one missing input | Demo Scene 2 |
| Refuses + escalates safely | Demo Scene 3 |
| Catches its own hallucination | Demo Scene 4 |
| Cannot touch OT | Demo Scene 5 / `boundary-test` |
| Full architecture | [ARCHITECTURE.md](../ARCHITECTURE.md) |
| The final output | [RECOVERY_WORK_PACKAGE.md](RECOVERY_WORK_PACKAGE.md) |

> **The story in one line:** a messy shop-floor goal goes in; a safe, cited, human-approved recovery package comes out; the machines stay protected; the next shift is informed; and every step is on the audit record.

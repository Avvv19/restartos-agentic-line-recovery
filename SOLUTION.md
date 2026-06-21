# Restart OS — the solution to Problem Statement 9

## The problem, reverse-engineered
> "Build an AI agent that solves a real, specific pain in manufacturing. Not a chatbot.
> Not a dashboard. An agent that takes a goal, makes decisions, calls tools, and produces
> an outcome a human would otherwise do by hand."

The specific pain we chose: **unplanned line-down fault resolution.** When a line stops,
a senior maintenance tech today manually chases information across siloed systems
(SCADA/historian, CMMS, MES, OEM manuals, parts/ERP, MOC, shift notes), reasons out the
likely cause, writes a safe procedure, and raises a work order. It is slow, it is tribal,
and it bleeds money every minute.

**Restart OS does that job.** Goal in → outcome out:
```
"Line 3 filler down, alarm A-220 — get it running safely, fast."
        ↓
verified Recovery Work Package: likely cause + evidence trail + safe (LOTO) procedure
+ parts + human-approved CMMS work order + QC plan + shift handover.
```

## Why it is an agent (not a chatbot, not a dashboard)
| Requirement | How Restart OS satisfies it |
|---|---|
| Takes a **goal** | An `Incident` contract (asset, symptom, alarm, $/hr) — not a chat prompt. |
| Makes **decisions** | Which tool to call next (agentic loop); the ranked differential; act vs **abstain**; the economics-routed gate. |
| Calls **tools** | A typed toolbelt over the plant silos (`tools.py`): historian, CMMS, manual RAG, MOC, parts, safety, MES, labour, shift notes, security. |
| Produces an **outcome** | Idempotent **writes to IT** (CMMS work order, parts reservation, QC plan, notifications) — the artifact a human assembled by hand. |
| Not a chatbot | There is no free chat. The surface is a run + approval cockpit. |
| Not a dashboard | No KPI charts. The human has two jobs: verify the reasoning, give go/no-go. |

## The flow (what actually executes)
`goal → resolve asset (join PI tag ↔ SAP funcloc ↔ OEM model) → agentic tool-use loop
(model/policy picks each tool, gathers cited, trust-weighted evidence) → differential
diagnosis (model-driven; offline = real weighted inference) → plan grounded to the manual
→ cross-model verifier (refutes; every citation must resolve; catches the planted bad
citation and forces a re-plan) → automated safety pre-check (LOTO) → role-matched,
economics-routed human gate → idempotent IT writes → monitor + capture known-fix →
tamper-evident hash-chained audit over everything.`

## The three non-negotiables (enforced in code, not prompts)
1. **Safety by construction** — the capability set makes OT writes impossible
   (`security.py`; `assert_capability(OT, WRITE)` raises). Restart/LOTO are human-only.
2. **Grounded or it doesn't ship** — the cross-model verifier resolves every citation
   against the real manual; unresolved → blocked (`verify.py`). 100% citation resolution,
   0 hallucinated parts on the eval.
3. **It can say "I don't know"** — abstain is a first-class decision; low confidence,
   unresolved contradiction, or an ungroundable plan → escalate, not guess.

## See it work — three ways, increasing realism
1. **`ui/restartos_live.html`** — double-click. The agent **runs live in your browser**,
   computing every step from the real plant data. Change the asset/alarm/$/hr and it
   recomputes; type an unknown asset and it abstains. No server, no key. (This is genuine
   computation, not a replay.)
2. **`make serve`** (Python, stdlib) — the real engine over the full 750-file dataset at
   `http://localhost:8000`; each run recomputes server-side.
3. **`make serve` + API keys in `.env`** — now the **model** chooses the tools and makes
   the diagnosis (`reasoned by anthropic:claude-opus-4-8`), with a different family
   verifying. This is the production path.

## Proof it is real (run `make test` → 14/14)
- `test_diagnosis_is_model_driven_when_a_model_is_present` — with a model, the work order's
  confidence is the **model's** number (0.79), not the heuristic (0.87).
- `test_agentic_loop_model_chooses_the_tool_sequence` — the model picks the exact tools.
- `test_ot_write_is_forbidden_by_construction` — OT writes raise.
- `test_verifier_catches_hallucinated_citation_then_replans`, `..._abstains_on_unknown_asset`,
  `..._it_writes_are_idempotent`, `..._audit_chain_is_tamper_evident`,
  `..._rest_connectors_real_http`, `..._rag_retrieves_real_manual_section`, and more.

## What is genuinely real vs. environment-gated
- **Real & tested:** orchestration (LangGraph), agentic tool loop, evidence graph, the
  verifier's groundedness check, safety pre-check, gate, idempotent IT writes, hash-chained
  audit, REST connectors (proven over live HTTP), semantic RAG, the in-browser engine.
- **Gated on your environment:** live LLM reasoning needs your API key; live plant data
  needs your PI/CMMS endpoints (`RESTARTOS_LIVE=1`). Both are one-line switches.

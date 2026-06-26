# Restart OS — Solution Architecture

## The problem
> When a production line goes down, a senior maintenance tech spends 45–90 minutes
> chasing data across 8 siloed systems just to write the work order. Restart OS is an
> autonomous agent that takes a goal, makes decisions, calls tools, and produces the
> outcome a human would otherwise assemble by hand.

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

## Why it is an autonomous agent
| Requirement | How Restart OS satisfies it |
|---|---|
| Takes a **goal** | A freeform operator report ("L3 filler keeps stopping, bottles backing up at the capper, A-220, restart before 3 PM") parsed into an `Incident` (`intake.py`), or a structured `Incident` contract directly. |
| Makes **decisions** | Which tool to call next (agentic loop); the ranked differential; first-fault isolation; **ACT vs ABSTAIN vs NEED_MORE_INFO**; the economics-routed gate. |
| Calls **tools** | A typed toolbelt over the plant silos (`tools.py`): historian, CMMS, manual RAG, MOC, parts, safety, MES, labour, shift notes, security. |
| Produces an **outcome** | Idempotent **writes to IT** (CMMS work order, parts reservation, QC plan, notifications) plus a **Decision Contract** every run, and an **Escalation Packet** when blocked — the artifacts a human assembled by hand. |
| Human-in-the-loop | The operator cockpit shows full reasoning, the evidence board, and the decision contract; the human verifies and gives go/no-go. |

## The flow (what actually executes)
`freeform operator report → parse to structured incident (intake.py) → resolve asset
(join PI tag ↔ SAP funcloc ↔ OEM model) → agentic tool-use loop (model/policy picks each
tool, gathers cited, trust-weighted evidence; mines repeat-failure patterns + captures
tribal knowledge) → differential diagnosis (model-driven; offline = real weighted
inference) → first-fault isolation (root fault vs downstream symptom) → plan grounded to
the manual → cross-model verifier (refutes; every citation must resolve; catches the
planted bad citation and forces a re-plan) → automated safety pre-check (LOTO) →
ACT / ABSTAIN / NEED_MORE_INFO → role-matched, economics-routed human gate → idempotent
IT writes → Decision Contract (or Escalation Packet if blocked) → monitor + capture
known-fix → tamper-evident hash-chained audit over everything.`

## The three non-negotiables (enforced in code, not prompts)
1. **Safety by construction** — the capability set makes OT writes impossible
   (`security.py`; `assert_capability(OT, WRITE)` raises). Restart/LOTO are human-only.
2. **Grounded or it doesn't ship** — the cross-model verifier resolves every citation
   against the real manual; unresolved → blocked (`verify.py`). 100% citation resolution,
   0 hallucinated parts on the eval.
3. **It can say "I don't know" — or "I need one more thing"** — the decision is
   three-way: **ACT** (confident, verified, safe), **ABSTAIN** (low confidence,
   unresolved contradiction, or an ungroundable plan → escalate with a useful packet),
   or **NEED_MORE_INFO** (one specific human-supplied input would unblock the call →
   ask for exactly that). A blocked run still produces an Escalation Packet with the
   next human step. Every run ends with a Decision Contract stating what the agent may
   do, what it is forbidden from doing (any OT write), and what a human must approve.

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

## Proof it is real (run `make test` → 35 passed, 4 skipped)
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

# 📦 The Recovery Work Package — what the agent actually hands you

> **In one line:** when a line goes down, Restart OS doesn't reply with a chat message or a chart. It produces the *same stack of paperwork and decisions a senior maintenance tech would assemble by hand* — in about 30 seconds — and waits for a human to press **Approve**.

If you've ever worked a shop floor, you know the drill when a machine stops: somebody runs around collecting facts, figures out what's wrong, writes the work order, reserves the part, finds the right person, plans the quality check, and scribbles a note for the next shift. That whole bundle is the **Recovery Work Package**. Restart OS builds it for you.

Think of it like a really good shift lead who has already done the legwork and put a clean folder on your desk. You still decide. You just don't have to *gather*.

---

## 🗂️ What's inside the folder

Every piece below is a real field the engine fills in (`restartos/domain.py` → `WorkPackage`, surfaced by `WorkPackage.artifacts()`). Here's each one in plain English, then what it's called in the code.

| The artifact | What it means to a human | In the code |
|---|---|---|
| 🎯 **Root cause summary** | "Here's what I think actually broke, and how sure I am." | `likely_cause` + `confidence` |
| ⏱️ **First-fault timeline** | "Out of all the alarms screaming at once, *this* is the one that started it. The rest are echoes." | `causal_chain` |
| 🔎 **Evidence board** | "Here's everything I checked, what I found, and whether it agrees or disagrees." | `evidence_trail` |
| ✅ **Restart checklist** | "Before you press start, these boxes must be ticked." | `restart_readiness` |
| 📝 **Work order draft** | "Here's the work order, already filled in — asset, cause, time estimate, risk level." | `work_order_draft` |
| 🔧 **Parts reservation** | "This is the part you'll need, how many are on the shelf, and which bin." | `parts_request` |
| 👷 **Technician assignment** | "This person is on shift, certified, and has fixed this before." | `work_order_draft.tech` |
| 🧪 **QC sampling plan** | "After restart, check this many units this way before you trust the line." | `qc_sampling_plan` |
| 🔁 **Shift handoff** | "Here's what the next shift needs to watch, what's unfinished, and what's risky." | `shift_handoff` |
| 🧾 **Maintenance patterns** | "Heads up — this exact fault has happened before. Here's the pattern." | `maintenance_patterns` |
| 💡 **Tribal-knowledge notes** | "The crew's informal know-how, captured — but marked *unverified* until a lead confirms it." | `knowledge_candidates` |
| 💰 **Economics** | "Here's the downtime and money this saves, adjusted for how confident I am." | `economics` |
| 📜 **Decision contract** | "Here's exactly what I'm allowed to do, what I'm *forbidden* to do, and what needs your signature." | `decision_contract` |
| 🚨 **Escalation packet** | *(only if blocked)* "I couldn't safely act — here's everything the next human needs to take over." | `EscalationPacket` |

---

## 🎯 1. Root cause summary

The agent's best single answer, with a confidence number **and** a built-in honesty check called *"what would change my mind."*

```
Likely cause : Nozzle clog   (ISO 14224 code 1.2.1)
Confidence   : 0.89
Would change my mind: flow normalizes after a CIP flush without a nozzle swap
```

That last line matters. A trustworthy diagnosis says what evidence would *disprove* it — not just what supports it.

## ⏱️ 2. First-fault timeline

A real plant failure sets off a *storm* of alarms in a few seconds. The loudest one is usually **not** the real problem. The classic trap: bottles pile up at the capper, everyone runs to the capper — but the capper is fine. The *filler* upstream quietly stopped flowing.

```
First actionable fault: Fill-nozzle clog (product solids)
Chain: clog → low flow → high head pressure → servo overload → bottle backup at capper
The capper backup is downstream. Fix the head, not the symptom.
```

## 🔎 3. Evidence board

Everything the agent checked, shown like a detective's pin-board: each fact has a **source**, a **trust level**, how **fresh** it is, and whether it **supports** or **contradicts** the diagnosis. Contradictions are shown, never quietly averaged away.

## 📝 4–9. The operational documents

These are the documents a tech would otherwise type by hand:

- **Work order draft** — asset, cause, ISO code, risk class, time estimate, number of techs.
- **Parts reservation** — e.g. `8200-NZ · 2 on hand · bin A4`.
- **Technician assignment** — who's on shift, certified, and has done this repair before.
- **QC sampling plan** — e.g. `AQL 2.5 · 5 units post-restart · check fill volume`.
- **Restart checklist** — LOTO removed, CIP complete, flow verified, QC pass, supervisor sign-off.
- **Shift handoff** — see below; this one is special.

## 🔁 The shift handoff (why it gets its own spotlight)

Handing knowledge to the next shift is one of the hardest, most-skipped jobs in manufacturing. The handoff here is built to be *immediately useful*:

```
What happened          : Line 3 filler A-220 — likely nozzle clog
What was checked       : Historian, CMMS, Manual, Parts, Safety, HR, MES, MOC, Shift notes
What was NOT checked   : (anything the agent couldn't reach)
Monitor next shift     : Watch filler flow for 45 min; if it drops below 36 L/min,
                         do NOT restart again — inspect the nozzle/gasket.
Parts reserved (not yet installed): 8200-NZ
Safety / LOTO          : LOTO-FILL-03 required; a human must CONFIRM completion.
Unresolved risks       : This fault recurred 15× — repeated cleaning isn't fixing root cause.
```

## 📜 The decision contract (the trust anchor)

Every run ends with a plain, four-part statement so nobody has to guess what the AI is doing:

```
Decision           : ACT
Allowed next action : Create the recovery work package (pending human approval)
Approved IT actions : CMMS work order · ERP parts · QMS QC plan · Slack/Teams notify
FORBIDDEN (in code) : Any PLC/SCADA/OT write · directly restarting the line
Human approval req  : yes
Risk class          : LOTO_PHYSICAL
Audit id            : ad4fcb0abc1e
```

## 🚨 If the agent is blocked — the escalation packet

A safe refusal is only useful if it tells the next person what to do. So a blocked run still produces:

```
Reported     : "the filler keeps stopping, bottles backing up at the capper"
Checked      : (whatever evidence it managed to gather)
Missing      : line / asset id, live historian readings
Likely cause : undetermined
Route to     : shift lead
NEXT STEP    : State which line the filler is on, or scan the asset QR tag.
```

---

## 👀 Where you see it

- **Live in the browser:** the demo cockpit at `/demo` renders every artifact above as clean cards. See [DEMO_GUIDE.md](DEMO_GUIDE.md).
- **As data:** `WorkPackage.artifacts()` returns the whole package as named JSON for any downstream system.

The point of the whole product, in one sentence: **messy shop-floor goal in → a safe, cited, human-approvable recovery folder out.**

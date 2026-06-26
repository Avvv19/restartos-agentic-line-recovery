# 🔒 The Safety Model — why this agent cannot crash your line

> **The first question any plant person asks about an AI on the factory floor is:** *"Can it touch the machines?"* The answer here is a hard, provable **no** — and this page explains exactly why, in plain language first and code second.

---

## 🧠 The one idea to take away

Restart OS is split into two worlds, like a hospital with a glass wall between the **observation room** and the **operating theatre**:

- 🟦 **The OT world** (Operational Technology) — the PLCs, SCADA, sensors, and the machine controls that actually move metal. Restart OS can **look, but never touch.** Read-only. Always.
- 🟩 **The IT world** (business systems) — the work-order system, parts inventory, quality system, and chat. Restart OS can **write here — but only after a human says yes.**

The line itself is never restarted by software. A person does that, by hand, after reading the agent's proposal. The agent does the paperwork; the human keeps the keys.

---

## 🚧 Five layers of protection (defense in depth)

Think of it like a bank vault — not one lock, but several, each catching what the last might miss.

### 1. 🛑 OT writes are impossible *by construction*

This is the big one. It is **not** a polite instruction in a prompt that a clever model might talk its way around. It's a capability check in code that **raises an error** the moment anything tries to write to a machine.

```python
# restartos/security.py
def assert_capability(plane, access):
    if plane in (Plane.OT_CONTROL, Plane.OT_OPS) and access == Access.WRITE:
        raise OTWriteForbidden(...)        # there is simply no allowed path
```

You can watch this fire yourself:

```bash
python -m restartos.cli boundary-test
#  write to OT_CONTROL blocked -> OTWriteForbidden
#  write to OT_OPS     blocked -> OTWriteForbidden
```

Or open the demo cockpit's **Scene 5**, where the agent literally tries `write_plc_speed(Line 3, 60%)` and gets stopped cold.

### 2. 🔍 The cross-model verifier — a second opinion that tries to *disprove* the plan

After the agent writes a recovery plan, a **different AI model family** checks it — and it's told to be a skeptic, not a cheerleader. Every step and every part number must point to a *real* manual passage or inventory row. One unsupported claim and the plan is **blocked**.

Why a *different* model? Because if the same model both writes and grades its own homework, you get false confidence. Two independent families = a genuine second opinion (`restartos/verify.py`).

### 3. 🦺 The automated safety pre-check

Before anything is proposed to a human, the engine checks the safety basics: Is there a LOTO (lockout-tagout) procedure? Does the plan skip it? Does it contradict the manual's safety section? If the work is physical and LOTO is missing, the plan **fails the pre-check** and the agent refuses.

> Important nuance: the agent can *document* the LOTO procedure, but **only a human can confirm LOTO is actually done.** Software never marks a safety step "complete."

### 4. 🚪 The authorization gate — the right person, for the right risk

Not every action needs the same sign-off. A low-risk work-order draft is different from a high-risk physical repair. The gate routes each action to the correct **role** and, for serious risks, requires an **e-signature** (21 CFR Part 11 style for regulated plants). The decision threshold even scales with how expensive the downtime is — governance *is* the ROI (`restartos/gate.py`, `config/authorization_matrix.yaml`).

### 5. 🤔 The agent can say "I don't know" or "I need one thing"

A safe agent refuses when it should. Restart OS has **three** outcomes, not one:

| Decision | Plain meaning |
|---|---|
| ✅ **ACT** | Enough evidence, verifier passed, safety passed → propose it for human approval. |
| 🟠 **NEED_MORE_INFO** | So close — one specific missing input would unblock it. Ask for *exactly* that. |
| 🔴 **ABSTAIN** | Evidence is weak, contradictory, or ungroundable → refuse, and hand over a useful escalation packet. |

A refusal is never a dead end — it always produces a next step for the next human.

---

## 🧾 Every run signs a Decision Contract

So there's never any doubt about what the agent did, every single run ends with a contract that states, in plain terms:

- ✅ what it **may** do next,
- ✅ which IT actions a human **may approve** (CMMS / ERP / QMS / notify),
- ⛔ what it is **forbidden** from doing (any PLC / SCADA / OT write, restarting the line),
- 📋 the evidence it used and the evidence still missing,
- 🪪 the risk class and a tamper-evident **audit id**.

---

## 🔗 The tamper-evident audit trail

Every step the agent takes is recorded in a **hash-chained** log (`restartos/audit.py`) — like a paper ledger where each page is sealed to the one before it. If anyone alters a past entry, the chain breaks and it shows. When something goes wrong at 3 a.m., you can replay *exactly* what the agent saw and decided.

---

## 🟡 And finally: the calibration window

Even with all of the above, Restart OS is **forbidden from acting autonomously on a real line until it spends 3–4 weeks in shadow mode** on your plant's own incidents, with a human reviewing every proposal, until its agreement-rate metrics clear. An AI that has never seen *your* plant hasn't earned the right to act on it yet. See [CALIBRATION.md](CALIBRATION.md).

---

### The whole safety story in one breath

> It can read the machines but never command them. It double-checks itself with a second AI. It refuses unsafe or unsupported plans. It asks a qualified human before writing anything. It records everything so you can audit it. And it won't go live on your line until it has proven itself for a month. **The agent does the thinking and the paperwork; a human always keeps the keys to the line.**

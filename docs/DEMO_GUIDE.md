# 🎬 Demo Guide — five scenes, five minutes

> This guide is for anyone evaluating Restart OS — including non-technical reviewers. Each scene is **one click**, runs the **real engine** (not a fake screenshot), and proves a different property of a trustworthy manufacturing agent.

---

## ▶️ How to run it

```bash
# 1. get the code
git clone https://github.com/Avvv19/restartos-agentic-line-recovery
cd restartos-agentic-line-recovery

# 2. (first time only) build the simulated plant + the demo scenes
pip install -r requirements.txt
python dataset/generate.py        # ~750 realistic, messy plant files
python -m restartos.cli demo      # build the five deterministic scenes

# 3. open the demo cockpit
python -m restartos.server        # then visit http://localhost:8000/demo
```

No API keys needed. With no keys the engine runs **offline-deterministic** (a built-in mock model), so every scene gives the *same* result every time — exactly what you want when presenting. Add real keys in `.env` and the *same* flow uses live models instead.

> 💡 You don't even need the server to read the scenes: `python -m restartos.cli demo` writes a `_it_state/demo.json` artifact, and the cockpit can load it directly.

---

## 🟢 Scene 1 — "ACT": the happy path, end to end

**What you do:** click *Scene 1*.

**What you see:** a messy operator note —
> *"Line 3 filler keeps stopping after 20 min, bottles backing up at the capper, A-220, need a restart before 3 PM"* —
turns into a full, human-approvable Recovery Work Package.

**What it proves:** the agent takes a *goal* (not a form), decides which plant systems to check, gathers cited evidence, isolates the **first actionable fault**, scores how sufficient the evidence is, writes a verified plan, and stops at a green **Approve** button. Press it → work order, parts reservation, QC plan, and a technician page are produced.

**Watch for:** the toolbelt panel (every call is a real plant system), the evidence sufficiency score, the first-fault chain, and the green/red **Decision Contract** at the bottom.

---

## 🟠 Scene 2 — "NEED_MORE_INFO": it asks instead of guessing

**What you do:** click *Scene 2*.

**What you see:** a vaguer note —
> *"the filler keeps stopping and bottles are backing up at the capper"* —
with no line number. The agent doesn't guess and it doesn't give up. It asks for **exactly one thing**:
> *Need: line / asset id for the filler → State which line the filler is on, or scan the asset QR tag.*

**What it proves:** maturity. Weak AI demos either answer or fail. A real agent knows the single missing input that would unblock it, and asks for that and nothing more.

---

## 🔴 Scene 3 — "ABSTAIN": a safe, *useful* refusal

**What you do:** click *Scene 3*.

**What you see:** an unknown asset with weak evidence. The agent **refuses to act** — and produces an **escalation packet**: what was reported, what it checked, what's missing, who to route to, and the exact next human step.

**What it proves:** the agent says "I don't know" when it should — but a refusal is never a dead end. It hands the next human a head start.

---

## 🟣 Scene 4 — the verifier catches a hallucination

**What you do:** click *Scene 4*.

**What you see:** the planner cites a manual page that doesn't exist (`...§7.4 p.212`). In the reasoning trace you'll see:
> *verifier: REFUTED → citation does NOT resolve to a real manual passage — blocked; re-planning*

The agent then **fixes itself** and produces a grounded plan.

**What it proves:** the anti-hallucination guardrail is real. A second, independent AI model tries to *disprove* the plan, catches the made-up citation, and forces a re-plan — before any human ever sees it.

---

## ⛔ Scene 5 — OT write blocked by construction

**What you do:** click *Scene 5*.

**What you see:** an attempt to command the line directly —
> `write_plc_speed(Line 3, 60%)` → **⛔ BLOCKED**

— along with two more blocked attempts.

**What it proves:** the agent **cannot touch the machines.** OT writes aren't discouraged by a prompt; they're impossible in code. The agent can *recommend* a restart; only a human can *perform* one.

---

## 🗺️ What powers each scene (for the curious)

| Piece | File |
|---|---|
| Scene definitions + builder | `restartos/demo.py` |
| The cockpit UI | `ui/demo.html` |
| The decision engine (real) | `restartos/orchestration.py` |
| First-fault isolation | `restartos/causal.py` |
| Decision contract + escalation | `restartos/contracts.py` |
| Freeform note → incident | `restartos/intake.py` |
| OT/IT safety boundary | `restartos/security.py` |

Each scene's output is **real engine output** — the same run JSON the operator cockpit renders, including the decision contract, evidence sufficiency, recovery work package, IT actions, and the OT-block proof.

---

## ✅ The 30-second judge summary

| Scene | One-click proof |
|---|---|
| 1 | It **acts** — messy note → verified, approvable recovery package |
| 2 | It **asks** — one missing input, requested precisely |
| 3 | It **refuses safely** — abstain + a useful escalation packet |
| 4 | It **catches itself** — verifier blocks a hallucination, re-plans |
| 5 | It **can't break things** — OT writes blocked by construction |

If you only have one minute: run Scene 1, then Scene 5. The first shows what it *does*; the second shows what it *can never do*.

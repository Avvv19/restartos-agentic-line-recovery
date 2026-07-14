# RestartOS

RestartOS is a personal simulated prototype for exploring evidence-driven agent workflows in a synthetic manufacturing line-recovery scenario. It reads generated plant data, gathers supporting context, drafts recovery artifacts, verifies constraints, and routes consequential actions to a human approval gate. It does not control machinery and has not been deployed in a plant.

## What the prototype demonstrates

- Deterministic offline model fallback and optional provider adapters
- LangGraph-oriented orchestration with an internal fallback
- Synthetic historian, parts, roster, safety, and shift-note data
- Qdrant-based semantic retrieval and optional Postgres memory
- Typed decision outcomes including approve, need-more-information, and abstain
- Human approval before simulated IT-side writes
- Append-oriented audit events and operational metrics
- Tests for pipeline behavior and mocked connector contracts

## Simulation boundary

All included industrial data is synthetic. Connector tests use mocked HTTP behavior and do not prove compatibility with a real historian, CMMS, HRIS, Slack workspace, or plant network. Configuration switches describe integration seams; they are not evidence of a live deployment.

The metrics endpoint calculates run counts, decision distributions, abstention rate, token usage, retrieval counts, and related diagnostics from available state. This README intentionally does not publish captured figures because no versioned reproducible run artifact is designated as canonical.

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env  # Windows
# cp .env.example .env  # macOS/Linux
```

Keep `RESTARTOS_LIVE=0` for the repository's supported simulated path. Do not add real plant credentials to a portfolio environment.

## Run

```bash
python -m restartos.cli run --auto-approve
python -m restartos.cli eval
python -m restartos.cli boundary-test
```

To start the local service:

```bash
python -m restartos.server --port 8000
```

## Validate

```bash
pytest -q
```

Tests validate the included code and mock contracts. They do not establish industrial safety approval, operational reliability, real-system authorization, or production readiness.

## Safety constraints

- No machine actuation path is included.
- Consequential IT-side actions remain behind human approval.
- Missing or contradictory evidence should produce need-more-information or abstain outcomes.
- Real integration would require site-specific security, network, safety, validation, and change-control review.

## Limitations and future work

- Designate and save reproducible evaluation artifacts before publishing run results.
- Replace mocked connector tests with contract tests against approved sandboxes.
- Add a formal threat model and failure-injection suite.
- Validate authorization, audit retention, and rollback behavior in a controlled environment.
- Treat every real-plant integration as a separate engineering and safety project.

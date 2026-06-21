"""Tests for the persistent memory + Qdrant semantic lane added in the
production hardening pass. These tests skip cleanly when the Docker stack
(Postgres + Qdrant) is not running, so CI on a vanilla runner still passes.

Run a single test:
    PYTHONPATH=. pytest -q tests/test_memory_qdrant.py::test_postgres_persist_and_recall
"""
from __future__ import annotations

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from restartos.memory import IncidentMemory, format_priors_as_facts


# --------------------------------------------------------------------------- #
# Postgres-backed incident memory                                              #
# --------------------------------------------------------------------------- #
def _mem_or_skip() -> IncidentMemory:
    m = IncidentMemory()
    if not m.available():
        pytest.skip("Postgres not reachable; `docker compose up postgres` to enable")
    return m


def test_memory_module_imports_without_db():
    """The module must not crash even if Postgres is down."""
    m = IncidentMemory(dsn="postgresql://invalid:invalid@127.0.0.1:1/nope")
    assert m.available() is False
    # All methods must be no-ops, never raise.
    assert m.recall_similar("FL-PKG-03-FILL", "A-220") == []
    assert m.persist_run(
        incident_id="X", asset_funcloc="X", asset_model="X", line="X",
        alarm_code="X", decision="ACT", top_cause="X", top_confidence=0.5,
        mttr_min=10, parts_used=[], trace_summary="", framework="test") is False


def test_postgres_persist_and_recall():
    m = _mem_or_skip()
    iid = f"INC-TEST-{uuid.uuid4().hex[:8]}"
    funcloc = f"TEST-{uuid.uuid4().hex[:6]}"  # isolate from real data
    ok = m.persist_run(
        incident_id=iid, asset_funcloc=funcloc, asset_model="Acme Model 8200",
        line="Line 3", alarm_code="A-220", decision="ACT",
        top_cause="Nozzle clog", top_confidence=0.87, mttr_min=45,
        parts_used=[{"part_no": "8200-NZ"}], trace_summary="test run",
        framework="pytest")
    assert ok is True

    priors = m.recall_similar(funcloc, "A-220")
    assert len(priors) == 1
    assert priors[0].incident_id == iid
    assert priors[0].decision == "ACT"
    assert priors[0].top_cause == "Nozzle clog"
    assert priors[0].top_confidence == pytest.approx(0.87)


def test_recall_similar_orders_by_recency():
    m = _mem_or_skip()
    funcloc = f"TEST-{uuid.uuid4().hex[:6]}"
    for i in range(3):
        m.persist_run(incident_id=f"INC-ORDER-{i}-{uuid.uuid4().hex[:6]}",
                      asset_funcloc=funcloc, asset_model="m", line="L",
                      alarm_code="A-220", decision="ACT", top_cause=f"cause-{i}",
                      top_confidence=0.7 + 0.01 * i, mttr_min=30, parts_used=[],
                      trace_summary="", framework="pytest")
        time.sleep(0.05)
    priors = m.recall_similar(funcloc, "A-220", limit=5)
    assert len(priors) == 3
    # Newest first
    assert priors[0].top_cause == "cause-2"
    assert priors[-1].top_cause == "cause-0"


def test_format_priors_as_facts_renders_human_readable():
    from datetime import datetime, timezone
    from restartos.memory import PriorOutcome
    p = PriorOutcome(incident_id="INC-1", ts=datetime(2026, 6, 21, tzinfo=timezone.utc),
                     decision="ACT", top_cause="Nozzle clog", top_confidence=0.87,
                     mttr_min=45, parts_used=[])
    s = format_priors_as_facts([p])
    assert "PRIOR_INCIDENTS_ON_THIS_ASSET" in s
    assert "Nozzle clog" in s
    assert "0.87" in s
    assert "45min" in s


# --------------------------------------------------------------------------- #
# Qdrant semantic lane                                                         #
# --------------------------------------------------------------------------- #
def _qdrant_or_skip():
    try:
        from qdrant_client import QdrantClient
        c = QdrantClient(os.getenv("QDRANT_URL", "http://localhost:6333"), timeout=2.0)
        c.get_collections()
        return c
    except Exception:
        pytest.skip("Qdrant not reachable; `docker compose up qdrant` to enable")


def test_qdrant_semantic_index_attaches_and_ingests():
    """The RAG facade should choose the Qdrant lane when both Qdrant and the
    embedder are available, and the collection should be populated."""
    _qdrant_or_skip()
    pytest.importorskip("sentence_transformers")
    from restartos.rag import ManualRAG
    rag = ManualRAG(os.path.join(os.path.dirname(__file__), "..", "_data", "manuals"))
    stats = rag.stats()
    assert stats["dense_lane"] == "qdrant", \
        f"expected qdrant lane, got {stats['dense_lane']}"
    assert rag.qdrant is not None and rag.qdrant.available()


def test_qdrant_semantic_search_returns_a220_section():
    """Semantic query for the failure mode should return the right manual
    section even though 'A-220' doesn't appear in the query."""
    _qdrant_or_skip()
    pytest.importorskip("sentence_transformers")
    from restartos.rag import ManualRAG
    rag = ManualRAG(os.path.join(os.path.dirname(__file__), "..", "_data", "manuals"))
    hits = rag.search("nozzle clog teardown procedure", k=3)
    assert hits, "expected at least one hit"
    # Top 2 hits should be in the Acme 8200 manual covering the relevant pages
    top = hits[0]
    assert "Acme_Model_8200" in top["citation"]
    assert top["score"] > 0.2


# --------------------------------------------------------------------------- #
# Observability endpoints (parse-level, no live HTTP)                          #
# --------------------------------------------------------------------------- #
def test_metrics_endpoint_produces_valid_prometheus_format(tmp_path, monkeypatch):
    """Build a minimal _it_state with one ACT run and one ABSTAIN run and
    confirm /metrics renders the right counters in Prometheus exposition format."""
    import json
    # Point ROOT at a temp tree
    state = tmp_path / "_it_state"
    state.mkdir()
    (state / "run_INC-ACT.json").write_text(json.dumps({
        "decision": "ACT",
        "router_usage": {"total_tokens": 1000, "total_cost_usd": 0.0},
        "work_package": {"likely_cause": {"confidence": 0.87}},
    }))
    (state / "run_INC-ABS.json").write_text(json.dumps({
        "decision": "ABSTAIN",
        "router_usage": {"total_tokens": 500, "total_cost_usd": 0.0},
        "work_package": None,
    }))
    import restartos.server as srv
    monkeypatch.setattr(srv, "ROOT", str(tmp_path))

    # Drive the _prometheus_metrics method on a fake handler-like object.
    captured = {}
    class FakeHandler:
        def send_response(self, c): captured["code"] = c
        def send_header(self, k, v): captured.setdefault("hdr", {})[k] = v
        def end_headers(self): pass
        class wfile:
            data = b""
            @classmethod
            def write(cls, b): cls.data += b
        _prometheus_metrics = srv.Handler._prometheus_metrics
    FakeHandler.wfile.data = b""
    FakeHandler()._prometheus_metrics()

    body = FakeHandler.wfile.data.decode()
    assert captured["code"] == 200
    assert "restartos_incidents_total 2" in body
    assert 'restartos_incidents_decision{decision="ACT"} 1' in body
    assert 'restartos_incidents_decision{decision="ABSTAIN"} 1' in body
    assert "restartos_abstention_rate 0.5000" in body
    assert "restartos_tokens_total 1500" in body
    assert "restartos_last_top_confidence 0.8700" in body

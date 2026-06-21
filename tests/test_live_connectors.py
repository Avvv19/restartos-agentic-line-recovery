"""
Mocked-HTTP tests for the production connectors added to wire RestartOS to
real plant systems:
  * FiixCMMS         — Fiix Software REST CMMS
  * BambooHRIS       — BambooHR REST HRIS (HTTP Basic auth)
  * SlackNotifier    — Slack incoming-webhook notifier
  * Wired engine     — when env vars are set the engine actually calls them.

The tests spin up a real ThreadingHTTPServer on localhost and assert that
the connector hits the correct path, sends the expected payload + auth
headers, and handles the response shape.
"""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from restartos.connectors import (
    BambooHRIS, FiixCMMS, FiixParts, RESTSession, SlackNotifier,
    build_cmms, build_hris, build_notifier, build_parts_backend,
    load_datalayer_config,
)


# --------------------------------------------------------------------------- #
# Generic in-process HTTP server                                                #
# --------------------------------------------------------------------------- #
class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Recorder:
    """Records every request the connector makes so the test can assert on it."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.next_response: tuple[int, dict] = (200, {})
        self.lock = threading.Lock()


def _make_handler(rec: _Recorder, route_fn):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _record(self, method):
            n = int(self.headers.get("Content-Length", 0) or 0)
            body_raw = self.rfile.read(n) if n else b""
            try:
                body_json = json.loads(body_raw) if body_raw else {}
            except Exception:
                body_json = {"_raw": body_raw.decode(errors="ignore")}
            with rec.lock:
                rec.calls.append({
                    "method": method, "path": self.path,
                    "headers": {k: v for k, v in self.headers.items()},
                    "body": body_json,
                })
            return route_fn(method, self.path, body_json)

        def do_GET(self):
            code, body = self._record("GET")
            self._send(code, body)

        def do_POST(self):
            code, body = self._record("POST")
            self._send(code, body)

    return H


def _spin(rec: _Recorder, route_fn):
    handler = _make_handler(rec, route_fn)
    srv = _Server(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    return srv, base


# --------------------------------------------------------------------------- #
# FiixCMMS                                                                     #
# --------------------------------------------------------------------------- #
def test_fiix_cmms_history_and_create_work_order():
    rec = _Recorder()

    def route(method, path, body):
        if method == "GET" and path.startswith("/api/v3/WorkOrders"):
            return 200, {"objects": [
                {"ID": 101, "Description": "Nozzle clog", "Status": "Closed",
                 "DateOpened": "2026-05-10", "DateClosed": "2026-05-10"},
            ]}
        if method == "POST" and path == "/api/v3/WorkOrders":
            return 201, {"ID": 999, **body}
        return 404, {"error": "not found"}

    srv, base = _spin(rec, route)
    try:
        sess = RESTSession(base, token="fiix-test-token")
        client = FiixCMMS(sess)

        history = client.history("L3-FILL-101")
        assert isinstance(history, list) and len(history) == 1
        assert history[0]["Description"] == "Nozzle clog"
        # Verify the right endpoint and auth header were used
        get_call = next(c for c in rec.calls if c["method"] == "GET")
        assert get_call["path"].startswith("/api/v3/WorkOrders?")
        # urlencode uses '+' for spaces and %27 for the quote character.
        assert "AssetID+eq+%27L3-FILL-101%27" in get_call["path"]
        assert get_call["headers"].get("Authorization") == "Bearer fiix-test-token"

        # Create returns the server-issued ID
        wo = client.create_work_order(
            asset_id="L3-FILL-101",
            description="Replace nozzle kit",
            idempotency_key="INC-0001",
            priority=1,
        )
        assert wo["ID"] == 999
        post_call = next(c for c in rec.calls if c["method"] == "POST")
        assert post_call["body"]["AssetID"] == "L3-FILL-101"
        assert post_call["body"]["Priority"] == 1
        assert post_call["body"]["Source"] == "RestartOS"
        # Idempotency key is prefixed into description so duplicates are detectable
        assert "[ros:INC-0001]" in post_call["body"]["Description"]
    finally:
        srv.shutdown()


def test_fiix_parts_reserve_uses_external_ref():
    rec = _Recorder()

    def route(method, path, body):
        if method == "POST" and path == "/api/v3/PartsReservations":
            return 201, {"ID": 555, "Status": "Reserved", **body}
        return 404, {"error": "not found"}

    srv, base = _spin(rec, route)
    try:
        client = FiixParts(RESTSession(base, token="t"))
        resp = client.reserve(
            work_order_id="WO-999",
            lines=[{"part_no": "8200-NZ", "qty": 1}],
            idempotency_key="INC-0001",
        )
        assert resp["ID"] == 555
        post = next(c for c in rec.calls if c["method"] == "POST")
        assert post["body"]["WorkOrderID"] == "WO-999"
        assert post["body"]["ExternalRef"] == "INC-0001"
        assert post["body"]["Lines"][0]["PartID"] == "8200-NZ"
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- #
# BambooHRIS                                                                   #
# --------------------------------------------------------------------------- #
def test_bamboohr_shift_roster_basic_auth_and_cert_mapping(monkeypatch):
    rec = _Recorder()

    def route(method, path, body):
        if method == "GET" and "/employees/directory" in path:
            return 200, {"employees": [{"id": 1001}, {"id": 1002}]}
        if method == "GET" and "/employees/1001" in path:
            return 200, {
                "firstName": "Jane", "lastName": "Martin",
                "jobTitle": "Sr. Mechanical Tech",
                "department": "Line 3",
                "customField_Shift": "B",
                "workPhone": "+1-555-0101",
                "customCert_LOTO": "Yes",
                "customCert_MechL2": "Yes",
                "customCert_Electrical": "No",
                "customCert_Compressor": "No",
            }
        if method == "GET" and "/employees/1002" in path:
            return 200, {
                "firstName": "Kai", "lastName": "Patel",
                "jobTitle": "Maintenance Lead",
                "department": "Line 4",       # different line — filtered out
                "customField_Shift": "B",
                "workPhone": "+1-555-0102",
                "customCert_LOTO": "Yes",
                "customCert_MechL2": "Yes",
                "customCert_Electrical": "Yes",
                "customCert_Compressor": "No",
            }
        return 404, {}

    srv, base = _spin(rec, route)
    try:
        # Build the client via the factory so we exercise the Basic-auth wiring
        monkeypatch.setenv("RESTARTOS_LIVE", "1")
        monkeypatch.setenv("HRIS_BACKEND", "bamboo")
        monkeypatch.setenv("BAMBOO_BASE_URL", base)
        monkeypatch.setenv("BAMBOO_SUBDOMAIN", "acmeplant")
        monkeypatch.setenv("BAMBOO_API_KEY", "secret-bamboo-key")
        monkeypatch.setenv("BAMBOO_TOKEN_ENV", "BAMBOO_API_KEY")
        cfg = load_datalayer_config()
        hris = build_hris(cfg)
        assert hris is not None and isinstance(hris, BambooHRIS)

        rows = hris.shift_roster(line="Line 3")
        # Only jmartin (Line 3) survives the filter
        assert len(rows) == 1
        emp = rows[0]
        assert emp["name"].strip() == "Jane Martin"
        assert emp["line"] == "Line 3"
        assert emp["shift"] == "B"
        assert emp["cert_loto"] == "Y"
        assert emp["cert_mech_l2"] == "Y"
        assert emp["cert_electrical"] == "N"
        assert emp["cert_compressor"] == "N"

        # Verify HTTP Basic header on every request
        for c in rec.calls:
            auth = c["headers"].get("Authorization", "")
            assert auth.startswith("Basic "), f"missing Basic auth in {c['path']}"
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- #
# SlackNotifier                                                                #
# --------------------------------------------------------------------------- #
def test_slack_notifier_posts_to_webhook():
    rec = _Recorder()

    def route(method, path, body):
        return 200, {"ok": True}

    srv, base = _spin(rec, route)
    try:
        webhook = f"{base}/services/T000/B000/secret"
        notifier = SlackNotifier(webhook)
        resp = notifier.notify("jmartin", "WO-42 assigned: Nozzle clog")
        assert resp["ok"] is True
        post = next(c for c in rec.calls if c["method"] == "POST")
        assert post["body"]["text"] == "<@jmartin> WO-42 assigned: Nozzle clog"
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- #
# Wired-engine: ITActionPlane uses the live CMMS + notifier when env says so    #
# --------------------------------------------------------------------------- #
def test_engine_routes_writes_through_live_fiix_and_slack(monkeypatch, tmp_path):
    """When RESTARTOS_LIVE=1 + Fiix + Slack are configured, the engine's
    ITActionPlane must hit the live mocks (not the JSON fallback)."""
    # Skip if the dataset is missing — engine bootstrapping needs it
    data_root = os.path.join(os.path.dirname(__file__), "..", "_data")
    if not os.path.exists(data_root):
        pytest.skip("dataset not generated; run `python dataset/generate.py`")

    fiix_rec = _Recorder()
    slack_rec = _Recorder()

    def fiix_route(method, path, body):
        if method == "GET" and path.startswith("/api/v3/WorkOrders"):
            return 200, {"objects": []}
        if method == "POST" and path == "/api/v3/WorkOrders":
            return 201, {"ID": 7777, **body}
        if method == "POST" and path == "/api/v3/PartsReservations":
            return 201, {"ID": 8888, "Status": "Reserved", **body}
        return 404, {}

    def slack_route(method, path, body):
        return 200, {"ok": True}

    fiix_srv, fiix_base = _spin(fiix_rec, fiix_route)
    slack_srv, slack_base = _spin(slack_rec, slack_route)
    try:
        monkeypatch.setenv("RESTARTOS_LIVE", "1")
        monkeypatch.setenv("CMMS_BACKEND", "fiix")
        monkeypatch.setenv("CMMS_BASE_URL", fiix_base)
        monkeypatch.setenv("CMMS_TOKEN", "fiix-secret")
        monkeypatch.setenv("CMMS_TOKEN_ENV", "CMMS_TOKEN")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", f"{slack_base}/hook")
        monkeypatch.setenv("SLACK_WEBHOOK_ENV", "SLACK_WEBHOOK_URL")

        from restartos.domain import GateOutcome, Incident, Severity
        from restartos.orchestration import RestartOSEngine

        # Point engine state at a tmpdir to keep the test isolated
        eng = RestartOSEngine(data_root=str(tmp_path))
        # Override data_root carefully — keep using the real _data fixture
        eng.dr.root = data_root  # type: ignore[attr-defined]

        assert isinstance(eng._live_cmms, FiixCMMS)
        assert isinstance(eng._live_parts, FiixParts)
        assert isinstance(eng._live_notifier, SlackNotifier)

        # Run a high-confidence ACT scenario. Even if the verifier abstains,
        # the engine will still issue the abstain-notify via the live notifier,
        # so we can validate the wiring either way.
        inc = Incident("Line 3 filler", "down", "Line 3", "A-220",
                       downtime_rate_per_hr=10_000, severity=Severity.HIGH)
        res = eng.run(inc, approver=lambda d: GateOutcome.APPROVED)

        # At minimum, Slack should have received at least one webhook POST
        assert any(c["method"] == "POST" for c in slack_rec.calls), \
            "engine did not call the live Slack notifier"

        # If we reached ACT, Fiix must have a WorkOrder POST recorded
        if res.decision.value == "ACT":
            assert any(c["method"] == "POST" and c["path"] == "/api/v3/WorkOrders"
                       for c in fiix_rec.calls), \
                "engine ACTed but did not POST to Fiix CMMS"
    finally:
        fiix_srv.shutdown()
        slack_srv.shutdown()

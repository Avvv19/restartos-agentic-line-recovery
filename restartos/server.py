"""
restartos.server
===============
A tiny stdlib HTTP server (no new dependencies) that runs the REAL engine in
process and serves the presenter UI. Built for a live, recordable demo.

  python -m restartos.server            # http://localhost:8000
  python -m restartos.server --port 9000

Endpoints (all JSON unless noted):
  GET  /                 -> ui/app.html (the presenter UI)
  POST /api/run          -> run a fault-to-fix; body: {hint,symptom,line,alarm,rate,auto_approve}
  GET  /api/eval         -> run the labeled fault-set evals
  GET  /api/providers    -> provider availability + anti-collusion routing
  GET  /api/rag?q=...    -> semantic retrieval over manuals
  GET  /api/boundary     -> prove OT writes are forbidden
  GET  /api/dataset      -> dataset manifest stats (file count, formats, defects)
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .domain import Incident, GateOutcome, Severity, to_jsonable
from .orchestration import RestartOSEngine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "ui", "index.html")
WORKBENCH = os.path.join(ROOT, "ui", "workbench.html")
CONFIG = os.path.join(ROOT, "config")


def _engine() -> RestartOSEngine:
    return RestartOSEngine(config_dir=CONFIG, data_root=os.path.join(ROOT, "_data"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _prometheus_metrics(self):
        """Aggregate run artifacts in _it_state/ into Prometheus-format counters.
        Cheap to compute (reads JSON files on each scrape). Suitable for a
        Grafana/Prometheus pull at 15s intervals on a single-instance deployment.
        """
        import glob
        from .memory import IncidentMemory
        state_dir = os.path.join(ROOT, "_it_state")
        runs = sorted(glob.glob(os.path.join(state_dir, "run_INC-*.json")))
        n_total = n_act = n_abstain = tokens_total = 0
        cost_total = 0.0
        last_conf = None
        for fp in runs:
            try:
                d = json.load(open(fp, encoding="utf-8"))
            except Exception:
                continue
            n_total += 1
            dec = d.get("decision", "")
            if dec == "ACT":
                n_act += 1
            elif dec == "ABSTAIN":
                n_abstain += 1
            ru = d.get("router_usage", {}) or {}
            tokens_total += int(ru.get("total_tokens", 0) or 0)
            cost_total += float(ru.get("total_cost_usd", 0) or 0)
            wp = d.get("work_package") or {}
            lc = wp.get("likely_cause") or {}
            if lc.get("confidence") is not None:
                last_conf = float(lc["confidence"])
        memory_n = IncidentMemory().stats().get("incidents", 0) if IncidentMemory().available() else 0
        abst_rate = (n_abstain / n_total) if n_total else 0.0
        lines = [
            "# HELP restartos_incidents_total Total incidents processed",
            "# TYPE restartos_incidents_total counter",
            f"restartos_incidents_total {n_total}",
            "# HELP restartos_incidents_decision Incidents by decision outcome",
            "# TYPE restartos_incidents_decision counter",
            f'restartos_incidents_decision{{decision="ACT"}} {n_act}',
            f'restartos_incidents_decision{{decision="ABSTAIN"}} {n_abstain}',
            "# HELP restartos_abstention_rate Fraction of incidents that abstained",
            "# TYPE restartos_abstention_rate gauge",
            f"restartos_abstention_rate {abst_rate:.4f}",
            "# HELP restartos_tokens_total Total LLM tokens consumed across all runs",
            "# TYPE restartos_tokens_total counter",
            f"restartos_tokens_total {tokens_total}",
            "# HELP restartos_cost_usd_total Cumulative LLM spend",
            "# TYPE restartos_cost_usd_total counter",
            f"restartos_cost_usd_total {cost_total:.4f}",
            "# HELP restartos_memory_incidents Incidents currently in Postgres memory",
            "# TYPE restartos_memory_incidents gauge",
            f"restartos_memory_incidents {memory_n}",
        ]
        if last_conf is not None:
            lines += [
                "# HELP restartos_last_top_confidence Top hypothesis confidence on the most recent run",
                "# TYPE restartos_last_top_confidence gauge",
                f"restartos_last_top_confidence {last_conf:.4f}",
            ]
        body = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path):
        if not os.path.exists(path):
            self._json({"error": "ui/app.html not found"}, 404)
            return
        body = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._html(INDEX)
            if u.path in ("/workbench", "/workbench.html"):
                return self._html(WORKBENCH)
            if u.path in ("/cockpit", "/cockpit.html", "/ui/cockpit.html"):
                fp = os.path.join(ROOT, "ui", "cockpit.html")
                if os.path.exists(fp):
                    return self._html(fp)
            if u.path in ("/api/latest_run", "/_it_state/latest_run.json"):
                fp = os.path.join(ROOT, "_it_state", "latest_run.json")
                if os.path.exists(fp):
                    body = open(fp, encoding="utf-8").read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body.encode("utf-8"))))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body.encode("utf-8"))
                    return
                return self._json({"error": "no run yet"}, 404)
            if u.path == "/api/memory":
                from .memory import IncidentMemory
                return self._json(IncidentMemory().stats())
            if u.path == "/api/dataset":
                mp = os.path.join(ROOT, "_data", "_manifest.json")
                m = json.load(open(mp)) if os.path.exists(mp) else {}
                return self._json({"file_count": m.get("file_count"),
                                   "formats": m.get("formats"),
                                   "scenario": m.get("scenario"),
                                   "recurring_fill_clog_wos": m.get("recurring_fill_clog_wos"),
                                   "intentional_defects": m.get("intentional_defects", [])})
            if u.path == "/api/providers":
                from .llm.providers import get_provider
                from .llm.router import ModelRouter, ProblemType
                names = ["nim", "groq", "gemini", "anthropic", "openai", "ollama", "mock"]
                avail = {n: get_provider(n).available() for n in names}
                r = ModelRouter(config_path=os.path.join(CONFIG, "model_routing.yaml"))
                ap, am, _ = r._pick(ProblemType.DEEP_DIAGNOSIS)
                r._author_provider = ap
                vp, vm, _ = r._pick(ProblemType.VERIFICATION)
                return self._json({"availability": avail,
                                   "author": f"{ap}/{am}", "verifier": f"{vp}/{vm}",
                                   "anti_collusion": ap != vp or sum(avail.values()) <= 1})
            if u.path == "/api/rag":
                from .rag import ManualRAG
                rag = ManualRAG(os.path.join(ROOT, "_data", "manuals"))
                query = (q.get("q") or ["nozzle clog"])[0]
                return self._json({"query": query, "stats": rag.stats(),
                                   "hits": rag.search(query, k=4)})
            if u.path == "/api/boundary":
                from .security import assert_capability, OTWriteForbidden, writable_planes
                from .domain import Plane, Access
                res = {"writable_planes": writable_planes(), "checks": []}
                for plane in (Plane.OT_CONTROL, Plane.OT_OPS):
                    try:
                        assert_capability(plane, Access.WRITE)
                        res["checks"].append({"plane": plane.value, "write_blocked": False})
                    except OTWriteForbidden:
                        res["checks"].append({"plane": plane.value, "write_blocked": True})
                return self._json(res)
            if u.path == "/api/eval":
                from .evals import run_evals
                return self._json(run_evals(config_dir=CONFIG,
                                            data_root=os.path.join(ROOT, "_data")))
            if u.path == "/healthz":
                from .memory import IncidentMemory
                m = IncidentMemory()
                qd_ok = False
                try:
                    from qdrant_client import QdrantClient
                    QdrantClient(os.getenv("QDRANT_URL", "http://localhost:6333"),
                                 timeout=2.0).get_collections()
                    qd_ok = True
                except Exception:
                    pass
                ok = m.available() and qd_ok
                return self._json({"ok": ok, "postgres": m.available(),
                                   "qdrant": qd_ok}, 200 if ok else 503)
            if u.path == "/metrics":
                return self._prometheus_metrics()
            return self._json({"error": "not found", "path": u.path}, 404)
        except Exception as e:
            import traceback
            return self._json({"error": str(e), "trace": traceback.format_exc()}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "{}") if n else {}
        try:
            if u.path == "/api/run":
                inc = Incident(
                    asset_hint=body.get("hint", "Line 3 filler"),
                    symptom=body.get("symptom", "down"),
                    line=body.get("line", "Line 3"),
                    alarm_code=body.get("alarm") or None,
                    downtime_rate_per_hr=float(body.get("rate", 10000)),
                    severity=Severity(body.get("severity", "HIGH")))
                approver = (lambda d: GateOutcome.APPROVED) if body.get("auto_approve", True) else None
                res = _engine().run(inc, approver=approver)
                return self._json(to_jsonable(res.__dict__))
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            import traceback
            return self._json({"error": str(e), "trace": traceback.format_exc()}, 500)


def main():
    ap = argparse.ArgumentParser("restartos.server")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"RestartOS cockpit  ->  http://{args.host}:{args.port}")
    print("  POST /api/run · GET /api/eval /api/providers /api/rag /api/boundary /api/dataset")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()

"""
restartos.connectors
====================
REAL REST connectors to plant systems, plus a live/dataset switch. Stdlib-only
(urllib) so there are no new dependencies. Each connector enforces the OT/IT
boundary by construction:

  * PIWebAPIHistorian  -> OT_OPS, READ-ONLY. There is no write method. A write
    attempt raises OTWriteForbidden via the capability check.
  * MaximoCMMS         -> IT_BUSINESS. Reads history; writes work orders only
    through gated, idempotent POST (the engine still gates before calling).

RESTSession adds bearer auth (token from env, never hardcoded), bounded retries
with exponential backoff, a simple client-side rate limit, and timeouts.

Switch with config/settings.yaml `live: true` + base URLs, or env:
  RESTARTOS_LIVE=1  PI_BASE_URL=...  PI_TOKEN_ENV=PI_TOKEN  CMMS_BASE_URL=...
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .domain import Access, Plane
from .security import assert_capability


class RESTSession:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 10.0,
                 max_retries: int = 3, rate_per_sec: float = 5.0,
                 verify_tls: bool = True) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = 1.0 / rate_per_sec if rate_per_sec else 0.0
        self.verify_tls = verify_tls
        self._last = 0.0

    def _throttle(self) -> None:
        dt = time.time() - self._last
        if dt < self.min_interval:
            time.sleep(self.min_interval - dt)
        self._last = time.time()

    def _request(self, method: str, path: str, params=None, body=None) -> dict:
        url = f"{self.base}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        last_err = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                ctx = None
                if url.startswith("https") and not self.verify_tls:
                    import ssl
                    ctx = ssl._create_unverified_context()
                with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as r:
                    raw = r.read().decode()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(0.2 * (2 ** attempt))   # backoff
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as e:
                last_err = e
                time.sleep(0.2 * (2 ** attempt))
        raise RuntimeError(f"{method} {url} failed after {self.max_retries} tries: {last_err}")

    def get(self, path: str, params=None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, body=body)


# --------------------------------------------------------------------------- #
class PIWebAPIHistorian:
    """OT_OPS, READ-ONLY. Mirrors HistorianAdapter.trend() so it's drop-in."""
    plane = Plane.OT_OPS

    def __init__(self, session: RESTSession) -> None:
        assert_capability(self.plane, Access.READ)   # read is allowed
        self.s = session

    def trend(self, tag: str, around_date: str = "") -> dict:
        # PI Web API: GET /streams/{webId}/recorded?startTime=...&endTime=...
        resp = self.s.get(f"/streams/{tag}/recorded",
                          params={"startTime": "*-1d", "endTime": "*"})
        items = resp.get("Items", [])
        vals = [it["Value"] for it in items
                if isinstance(it.get("Value"), (int, float)) and it.get("Good", True)]
        if not vals:
            return {"tag": tag, "status": "no_data"}
        baseline = sum(vals[: max(1, len(vals)//3)]) / max(1, len(vals)//3)
        return {"tag": tag, "baseline": round(baseline, 2), "peak": round(max(vals), 2),
                "trough": round(min(vals), 2), "n": len(vals), "stale_pts": 0,
                "missing_pts": 0, "uom": resp.get("UnitsAbbreviation", ""),
                "citation": f"PI:{tag}"}

    # NOTE: intentionally NO write method. Actuation is impossible by construction.


class MaximoCMMS:
    """IT_BUSINESS. Read WO history; create WO via gated, idempotent POST."""
    plane = Plane.IT_BUSINESS

    def __init__(self, session: RESTSession) -> None:
        self.s = session

    def history(self, funcloc: str) -> list[dict]:
        assert_capability(self.plane, Access.READ)
        resp = self.s.get("/maximo/api/os/mxwo",
                          params={"oslc.where": f'location="{funcloc}"',
                                  "oslc.select": "wonum,location,description,worktype"})
        return resp.get("member", resp.get("rdfs:member", []))

    def create_work_order(self, funcloc: str, description: str,
                          idempotency_key: str) -> dict:
        assert_capability(self.plane, Access.WRITE)   # gated upstream by the engine
        return self.s.post("/maximo/api/os/mxwo",
                           {"location": funcloc, "description": description,
                            "status": "WAPPR", "externalrefid": idempotency_key})


# --------------------------------------------------------------------------- #
class FiixCMMS:
    """IT_BUSINESS. Fiix Software CMMS REST API. Used at production trials
    because the free tier supports a single plant. Auth = Bearer; payload
    follows the Fiix /api/v3 schema.

    Endpoints used:
      GET  /api/v3/WorkOrders?filters=AssetID eq '<id>'   -> history
      POST /api/v3/WorkOrders                              -> create
    Idempotency: Fiix doesn't have a native key; we send our incident_id as
    `description` prefix so duplicates are detectable on re-run.
    """
    plane = Plane.IT_BUSINESS

    def __init__(self, session: RESTSession) -> None:
        self.s = session

    def history(self, asset_id: str) -> list[dict]:
        assert_capability(self.plane, Access.READ)
        resp = self.s.get("/api/v3/WorkOrders",
                          params={"filters": f"AssetID eq '{asset_id}'",
                                  "fields": "ID,Description,Status,DateOpened,DateClosed"})
        return resp.get("objects", resp.get("value", []))

    def create_work_order(self, asset_id: str, description: str,
                          idempotency_key: str, priority: int = 2) -> dict:
        assert_capability(self.plane, Access.WRITE)   # gated upstream
        body = {
            "AssetID": asset_id,
            "Description": f"[ros:{idempotency_key}] {description}",
            "Priority": priority,            # 1=critical, 2=high, 3=normal
            "Status": "Open",
            "Source": "RestartOS",
        }
        return self.s.post("/api/v3/WorkOrders", body)


class FiixParts:
    """ERP-equivalent: Fiix has a Parts module. Used for reservations."""
    plane = Plane.IT_BUSINESS

    def __init__(self, session: RESTSession) -> None:
        self.s = session

    def reserve(self, work_order_id: str, lines: list[dict],
                idempotency_key: str) -> dict:
        assert_capability(self.plane, Access.WRITE)
        body = {
            "WorkOrderID": work_order_id,
            "ExternalRef": idempotency_key,
            "Lines": [{"PartID": l["part_no"], "Quantity": l.get("qty", 1)}
                      for l in lines],
        }
        return self.s.post("/api/v3/PartsReservations", body)


# --------------------------------------------------------------------------- #
class BambooHRIS:
    """IT_BUSINESS. BambooHR REST API: shift roster + cert tracking.

    Endpoints:
      GET /api/gateway.php/<subdomain>/v1/employees/directory
      GET /api/gateway.php/<subdomain>/v1/employees/<id>?fields=...
    Auth: HTTP Basic with API key as username, "x" as password (BambooHR
    convention). The session is configured for this elsewhere.
    """
    plane = Plane.IT_BUSINESS

    # Map our internal cert keys to BambooHR custom-field names. Customers
    # override this on deployment to match their schema.
    DEFAULT_CERT_MAP = {
        "cert_loto": "customCert_LOTO",
        "cert_mech_l2": "customCert_MechL2",
        "cert_electrical": "customCert_Electrical",
        "cert_compressor": "customCert_Compressor",
    }

    def __init__(self, session: RESTSession, subdomain: str,
                 cert_map: dict | None = None) -> None:
        self.s = session
        self.sub = subdomain
        self.cert_map = cert_map or self.DEFAULT_CERT_MAP

    def shift_roster(self, line: str | None = None,
                     shift: str | None = None) -> list[dict]:
        """Return employees with their certs, optionally filtered by line/shift."""
        assert_capability(self.plane, Access.READ)
        directory = self.s.get(f"/api/gateway.php/{self.sub}/v1/employees/directory")
        rows = []
        for emp in directory.get("employees", []):
            eid = emp.get("id")
            if not eid:
                continue
            fields = ",".join(["firstName", "lastName", "jobTitle", "workPhone",
                               "department",  # we treat dept as line
                               "customField_Shift"] + list(self.cert_map.values()))
            detail = self.s.get(
                f"/api/gateway.php/{self.sub}/v1/employees/{eid}",
                params={"fields": fields})
            emp_line = detail.get("department") or ""
            emp_shift = detail.get("customField_Shift") or ""
            if line and line.lower() not in emp_line.lower():
                continue
            if shift and shift.lower() != emp_shift.lower():
                continue
            row = {
                "employee_id": str(eid),
                "name": (detail.get("firstName") or "") + " " + (detail.get("lastName") or ""),
                "role": detail.get("jobTitle") or "",
                "line": emp_line,
                "shift": emp_shift,
                "phone": detail.get("workPhone") or "",
            }
            for k, bamboo_field in self.cert_map.items():
                v = detail.get(bamboo_field, "")
                row[k] = "Y" if str(v).strip().lower() in ("yes", "y", "true", "1") else "N"
            rows.append(row)
        return rows


# --------------------------------------------------------------------------- #
class SlackNotifier:
    """IT_BUSINESS. Direct mention or channel post via incoming webhook.
    Plant convention: paged techs get a DM; everyone watches a channel.
    """
    plane = Plane.IT_BUSINESS

    def __init__(self, webhook_url: str) -> None:
        self.url = webhook_url

    def notify(self, recipient: str, text: str) -> dict:
        assert_capability(self.plane, Access.WRITE)
        # Webhook receives a POST with a JSON body. We bypass RESTSession
        # because Slack webhooks don't accept Authorization headers — the URL
        # itself is the secret. Use urllib directly here.
        import urllib.request
        body = json.dumps({"text": f"<@{recipient}> {text}"}).encode()
        req = urllib.request.Request(self.url, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5.0) as r:
                return {"ok": True, "status": r.status}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- #
@dataclass
class DataLayerConfig:
    # Live switch
    live: bool = False
    verify_tls: bool = True
    # Historian (PI Web API)
    pi_base_url: str = ""
    pi_token_env: str = "PI_TOKEN"
    # CMMS — pick one backend
    cmms_backend: str = "maximo"            # "maximo" | "fiix" | "sim"
    cmms_base_url: str = ""
    cmms_token_env: str = "CMMS_TOKEN"
    # HRIS (BambooHR)
    hris_backend: str = "sim"               # "bamboo" | "sim"
    hris_base_url: str = ""                 # e.g. https://api.bamboohr.com
    hris_subdomain: str = ""                # the org's BambooHR subdomain
    hris_token_env: str = "BAMBOO_API_KEY"
    # Slack notifier
    slack_webhook_env: str = "SLACK_WEBHOOK_URL"


def load_datalayer_config(settings: dict | None = None) -> DataLayerConfig:
    s = settings or {}
    return DataLayerConfig(
        live=os.getenv("RESTARTOS_LIVE", "1" if s.get("live") else "0") == "1",
        verify_tls=os.getenv("RESTARTOS_VERIFY_TLS", "1") == "1",
        pi_base_url=os.getenv("PI_BASE_URL", s.get("pi_base_url", "")),
        pi_token_env=os.getenv("PI_TOKEN_ENV", s.get("pi_token_env", "PI_TOKEN")),
        cmms_backend=os.getenv("CMMS_BACKEND", s.get("cmms_backend", "sim")).lower(),
        cmms_base_url=os.getenv("CMMS_BASE_URL", s.get("cmms_base_url", "")),
        cmms_token_env=os.getenv("CMMS_TOKEN_ENV", s.get("cmms_token_env", "CMMS_TOKEN")),
        hris_backend=os.getenv("HRIS_BACKEND", s.get("hris_backend", "sim")).lower(),
        hris_base_url=os.getenv("BAMBOO_BASE_URL", s.get("hris_base_url", "https://api.bamboohr.com")),
        hris_subdomain=os.getenv("BAMBOO_SUBDOMAIN", s.get("hris_subdomain", "")),
        hris_token_env=os.getenv("BAMBOO_TOKEN_ENV", "BAMBOO_API_KEY"),
        slack_webhook_env=os.getenv("SLACK_WEBHOOK_ENV", "SLACK_WEBHOOK_URL"))


def build_historian(dr, cfg: DataLayerConfig):
    """Return a live PI historian if configured, else the dataset adapter."""
    if cfg.live and cfg.pi_base_url:
        sess = RESTSession(cfg.pi_base_url, token=os.getenv(cfg.pi_token_env),
                           verify_tls=cfg.verify_tls)
        return PIWebAPIHistorian(sess)
    from .data import HistorianAdapter
    return HistorianAdapter(dr)


def build_cmms(cfg: DataLayerConfig):
    """Return a real CMMS client when CMMS_BASE_URL + backend are set, else None
    (the orchestrator falls back to the JSON-on-disk simulator)."""
    if not (cfg.live and cfg.cmms_base_url):
        return None
    sess = RESTSession(cfg.cmms_base_url, token=os.getenv(cfg.cmms_token_env),
                       verify_tls=cfg.verify_tls)
    if cfg.cmms_backend == "fiix":
        return FiixCMMS(sess)
    if cfg.cmms_backend == "maximo":
        return MaximoCMMS(sess)
    return None


def build_parts_backend(cfg: DataLayerConfig):
    if cfg.live and cfg.cmms_base_url and cfg.cmms_backend == "fiix":
        sess = RESTSession(cfg.cmms_base_url, token=os.getenv(cfg.cmms_token_env),
                           verify_tls=cfg.verify_tls)
        return FiixParts(sess)
    return None


def build_hris(cfg: DataLayerConfig):
    """Return a real HRIS client when configured."""
    if not (cfg.live and cfg.hris_backend == "bamboo" and cfg.hris_subdomain):
        return None
    api_key = os.getenv(cfg.hris_token_env)
    if not api_key:
        return None
    # BambooHR uses HTTP Basic with api_key as username + literal "x" as password.
    import base64
    basic = base64.b64encode(f"{api_key}:x".encode()).decode()
    sess = RESTSession(cfg.hris_base_url, verify_tls=cfg.verify_tls)
    # Inject the Basic auth header by overriding token logic with a hook
    orig = sess._request
    def _patched(method, path, params=None, body=None):
        url = f"{sess.base}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "Authorization": f"Basic {basic}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=sess.timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    sess._request = _patched
    return BambooHRIS(sess, subdomain=cfg.hris_subdomain)


def build_notifier(cfg: DataLayerConfig):
    """Return a Slack notifier when SLACK_WEBHOOK_URL is set."""
    url = os.getenv(cfg.slack_webhook_env)
    if cfg.live and url:
        return SlackNotifier(url)
    return None

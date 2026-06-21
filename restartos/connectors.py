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
@dataclass
class DataLayerConfig:
    live: bool = False
    pi_base_url: str = ""
    pi_token_env: str = "PI_TOKEN"
    cmms_base_url: str = ""
    cmms_token_env: str = "CMMS_TOKEN"
    verify_tls: bool = True


def load_datalayer_config(settings: dict | None = None) -> DataLayerConfig:
    s = settings or {}
    return DataLayerConfig(
        live=os.getenv("RESTARTOS_LIVE", "1" if s.get("live") else "0") == "1",
        pi_base_url=os.getenv("PI_BASE_URL", s.get("pi_base_url", "")),
        pi_token_env=os.getenv("PI_TOKEN_ENV", s.get("pi_token_env", "PI_TOKEN")),
        cmms_base_url=os.getenv("CMMS_BASE_URL", s.get("cmms_base_url", "")),
        cmms_token_env=os.getenv("CMMS_TOKEN_ENV", s.get("cmms_token_env", "CMMS_TOKEN")),
        verify_tls=os.getenv("RESTARTOS_VERIFY_TLS", "1") == "1")


def build_historian(dr, cfg: DataLayerConfig):
    """Return a live PI historian if configured, else the dataset adapter."""
    if cfg.live and cfg.pi_base_url:
        sess = RESTSession(cfg.pi_base_url, token=os.getenv(cfg.pi_token_env),
                           verify_tls=cfg.verify_tls)
        return PIWebAPIHistorian(sess)
    from .data import HistorianAdapter
    return HistorianAdapter(dr)

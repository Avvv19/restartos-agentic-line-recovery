"""
restartos.data.adapters
======================
READ-ONLY adapters over the plant silos. In production each class wraps a real
connector (PI Web API, SAP PM/BAPI, MES OData, file shares); here they read the
generated multi-format dataset. The interface is what matters: every adapter
returns facts + a *resolvable citation*, and exposes `.resolve_citation()` so
the verifier can confirm a cited source actually exists.

CRITICAL: nothing in this module can WRITE to OT. These are read lenses only.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

# Curated source-trust priors (a reading is evidence, not gospel).
SOURCE_TRUST = {
    "ASSET_REGISTRY": 0.95, "MANUAL": 0.92, "CMMS": 0.80, "MES": 0.78,
    "HISTORIAN": 0.70, "MOC": 0.85, "PARTS": 0.88, "SAFETY": 0.97,
    "SHIFT_NOTES": 0.45,   # tribal knowledge: useful lead, low trust
}


class DataRoot:
    """Resolves the dataset location once and shares it across adapters."""
    def __init__(self, root: Optional[str] = None) -> None:
        env = os.getenv("RESTARTOS_DATA")
        if root is None:
            root = env or os.path.join(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))), "_data")
        self.root = os.path.abspath(root)
        if not os.path.isdir(self.root):
            raise FileNotFoundError(
                f"dataset not found at {self.root}; run dataset/generate.py first")

    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)


# --------------------------------------------------------------------------- #
# Asset Resolver — the keystone. Joins PI tag <-> SAP funcloc <-> manual model. #
# --------------------------------------------------------------------------- #
@dataclass
class ResolvedAsset:
    funcloc: str
    line: str
    model: str
    mes_id: str
    criticality: str
    pi_tags: list[str]
    confidence: float
    notes: list[str]


class AssetRegistry:
    def __init__(self, dr: DataRoot) -> None:
        self.dr = dr
        self.rows: list[dict] = []
        with open(dr.path("asset_registry.csv"), encoding="utf-8") as f:
            self.rows = list(csv.DictReader(f))

    def resolve(self, hint: str) -> Optional[ResolvedAsset]:
        """Fuzzy-resolve an operator hint to a single asset. Tolerates the messy
        registry (e.g. FL-PKG-3-FILL vs FL-PKG-03-FILL, 'Acme 8200' vs 'Model 8200')."""
        h = hint.lower().strip()
        norm = re.sub(r"[^a-z0-9]", "", h)
        scored: dict[str, float] = {}
        notes: list[str] = []
        for r in self.rows:
            fl = r["sap_funcloc"]
            cand_norm = re.sub(r"[^a-z0-9]", "", fl.lower())
            score = 0.0
            if norm and (norm in cand_norm or cand_norm in norm):
                score += 0.6
            if r["pi_tag"] in h or r["pi_tag"] == h:
                score += 0.9
            for token in re.split(r"\W+", h):
                if token and token in fl.lower():
                    score += 0.2
                if token and len(token) > 1 and token in r["line"].lower().replace("line", ""):
                    score += 0.3
                if token in ("filler", "fill") and "FILL" in fl:
                    score += 0.4
                if token in ("capper", "cap") and "CAP" in fl:
                    score += 0.4
            if score:
                scored[fl] = max(scored.get(fl, 0.0), score)
        best = max(scored, key=lambda k: scored[k]) if scored else None
        if not scored or scored[best] < 0.5:
            return None   # too weak to be a real match -> caller should abstain
        # collapse funcloc spelling variants (FL-PKG-3-FILL ~ FL-PKG-03-FILL)
        canonical = self._canonical(best)
        if canonical != best:
            notes.append(f"registry spelling variant '{best}' -> canonical '{canonical}'")
        group = [r for r in self.rows
                 if self._canonical(r["sap_funcloc"]) == canonical]
        tags = sorted({r["pi_tag"] for r in group})
        g0 = group[0]
        conf = min(0.99, scored[best] / 1.5)
        return ResolvedAsset(canonical, g0["line"], g0["manufacturer_model"],
                             g0["mes_asset_id"], g0["criticality"], tags,
                             round(conf, 2), notes)

    @staticmethod
    def _canonical(funcloc: str) -> str:
        # normalize the line segment to 2 digits: FL-PKG-3-FILL -> FL-PKG-03-FILL
        m = re.match(r"(FL-[A-Z]+)-(\d+)-(.+)", funcloc)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{m.group(3)}"
        return funcloc


# --------------------------------------------------------------------------- #
class HistorianAdapter:
    """READ-ONLY trend access (OPC-UA / PI Web API in prod)."""
    def __init__(self, dr: DataRoot) -> None:
        self.dr = dr

    def trend(self, tag: str, around_date: str = "20260630") -> dict:
        files = sorted(glob.glob(self.dr.path("historian", f"{tag}_*.csv")))
        target = [f for f in files if around_date in f] or files[-1:]
        vals, stale, missing = [], 0, 0
        uom = ""
        for fp in target:
            with open(fp, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    uom = row.get("uom", uom)
                    if row["quality"] == "Bad" or row["value"] == "":
                        missing += 1
                        continue
                    if row["quality"] == "Stale":
                        stale += 1
                    try:
                        vals.append(float(row["value"]))
                    except ValueError:
                        missing += 1
        if not vals:
            return {"tag": tag, "status": "no_data", "missing": missing}
        baseline = sum(vals[: len(vals)//3]) / max(1, len(vals)//3)
        peak = max(vals)
        trough = min(vals)
        return {"tag": tag, "uom": uom, "baseline": round(baseline, 2),
                "peak": round(peak, 2), "trough": round(trough, 2),
                "n": len(vals), "stale_pts": stale, "missing_pts": missing,
                "citation": f"historian:{tag}@{around_date}"}


# --------------------------------------------------------------------------- #
class MESAdapter:
    def __init__(self, dr: DataRoot) -> None:
        self.dr = dr

    def downtime_for(self, mes_id: str) -> list[dict]:
        out = []
        with open(self.dr.path("mes", "oee_daily.csv"), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["mes_asset_id"] == mes_id and int(r["downtime_min"]) > 0:
                    out.append(r)
        return out

    def reason_codes(self) -> dict[str, str]:
        import xml.etree.ElementTree as ET
        t = ET.parse(self.dr.path("mes", "downtime_codes.xml"))
        return {c.get("id"): c.text for c in t.getroot()}


# --------------------------------------------------------------------------- #
class CMMSAdapter:
    def __init__(self, dr: DataRoot) -> None:
        self.dr = dr
        with open(dr.path("cmms", "work_orders.csv"), encoding="utf-8") as f:
            self.wos = list(csv.DictReader(f))

    def history(self, funcloc: str) -> list[dict]:
        return [w for w in self.wos if w["funcloc"] == funcloc]

    def recurring(self, funcloc: str) -> dict:
        h = self.history(funcloc)
        causes: dict[str, int] = {}
        for w in h:
            causes[w["cause"]] = causes.get(w["cause"], 0) + 1
        # surface duplicate WO ids with contradictory causes
        seen: dict[str, set] = {}
        conflicts = []
        for w in h:
            s = seen.setdefault(w["wo_id"], set())
            s.add(w["cause"])
        for wo_id, cset in seen.items():
            if len(cset) > 1:
                conflicts.append({"wo_id": wo_id, "causes": sorted(cset)})
        top = sorted(causes.items(), key=lambda kv: -kv[1])
        return {"counts": dict(top), "top_cause": top[0][0] if top else None,
                "n_wo": len(h), "conflicts": conflicts}

    def patterns(self, funcloc: str, window_days: int = 21) -> list[dict]:
        """Mine the work-order history for REPEAT-FAILURE signals — the thing a
        one-off troubleshooting helper never sees. Surfaces: same cause N times
        in a rolling window, the same part replaced over and over, a repair that
        failed again within ~24h, and symptom-only fixes that never resolve the
        root cause."""
        from collections import Counter
        from datetime import datetime

        def _date(s):
            for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
                try:
                    return datetime.strptime((s or "").strip(), fmt)
                except (ValueError, TypeError):
                    continue
            return None

        h = self.history(funcloc)
        by_cause: dict[str, list[dict]] = {}
        for w in h:
            by_cause.setdefault(w.get("cause", "?"), []).append(w)

        out: list[dict] = []
        for cause, wos in by_cause.items():
            if len(wos) < 2:
                continue
            dated = sorted([w for w in wos if _date(w.get("open_date"))],
                           key=lambda w: _date(w["open_date"]))
            # tightest rolling window containing the most occurrences
            window_max, i = 1, 0
            for j in range(len(dated)):
                while (_date(dated[j]["open_date"]) - _date(dated[i]["open_date"])).days > window_days:
                    i += 1
                window_max = max(window_max, j - i + 1)

            parts = Counter(p.strip() for w in wos
                            for p in (w.get("parts_used", "") or "").split(",") if p.strip())
            repeated_part = parts.most_common(1)[0][0] if parts else None

            # a repair that failed again within ~24h of the previous close
            failed_within_h = None
            for a, b in zip(dated, dated[1:]):
                close_a = _date(a.get("close_date")) or _date(a.get("open_date"))
                open_b = _date(b.get("open_date"))
                if close_a and open_b:
                    gap_h = (open_b - close_a).total_seconds() / 3600.0
                    if 0 <= gap_h <= 48 and (failed_within_h is None or gap_h < failed_within_h):
                        failed_within_h = round(gap_h, 1)

            actions = " ".join((w.get("action", "") or "").lower() for w in wos)
            symptom_only = (any(k in actions for k in ("flush", "clean", "reset", "adjust"))
                            and len(wos) >= 3)

            rec = (f"'{cause}' recurred {len(wos)}x on {funcloc} "
                   f"(up to {window_max}x within {window_days} days). ")
            if repeated_part:
                rec += f"Part {repeated_part} replaced repeatedly. "
            if symptom_only:
                rec += ("Repeated identical repair has not resolved the root "
                        "cause — inspect upstream wear before the next restart. ")
            if failed_within_h is not None:
                rec += f"A prior repair failed again within {failed_within_h}h. "

            out.append({"cause": cause, "occurrences": len(wos),
                        "window_days": window_days, "window_max": window_max,
                        "repeated_part": repeated_part,
                        "repair_failed_within_h": failed_within_h,
                        "symptom_only_fix": symptom_only,
                        "recommendation": rec.strip()})
        out.sort(key=lambda p: -p["occurrences"])
        return out


# --------------------------------------------------------------------------- #
_RAG_CACHE: dict = {}


class ManualAdapter:
    """Manual access + the all-important citation RESOLUTION check + semantic RAG."""
    def __init__(self, dr: DataRoot) -> None:
        self.dr = dr

    def rag(self):
        """Lazily build (and cache) a semantic index over all manuals (md + pdf)."""
        key = self.dr.path("manuals")
        if key not in _RAG_CACHE:
            from ..rag import ManualRAG
            _RAG_CACHE[key] = ManualRAG(key)
        return _RAG_CACHE[key]

    def semantic_section(self, model: str, query: str):
        """Find the manual section for a natural-language alarm/symptom over real
        docs. Returns the structured section dict if the top hit is in this model."""
        nums = re.findall(r"\d{3,}", model)
        for hit in self.rag().search(query, k=5):
            if nums and nums[0] in hit["citation"] and hit["section"]:
                sec = self.section(model, hit["section"])
                if sec:
                    sec["retrieval_score"] = hit["score"]
                    return sec
        return None

    def _model_file(self, model: str) -> Optional[str]:
        # 'Acme Model 8200' / 'Acme 8200' -> Acme_Model_8200.md (by number token)
        nums = re.findall(r"\d{3,}", model)
        for fp in glob.glob(self.dr.path("manuals", "*.md")):
            if nums and nums[0] in os.path.basename(fp):
                return fp
        return None

    def section(self, model: str, sec: str) -> Optional[dict]:
        fp = self._model_file(model)
        if not fp:
            return None
        text = open(fp, encoding="utf-8", errors="ignore").read()
        m = re.search(rf"## §{re.escape(sec)}\b.*?\(p\.(\d+)\)\n\n(.+?)(?=\n## |\Z)",
                      text, re.S)
        if not m:
            return None
        return {"model": model, "section": sec, "page": int(m.group(1)),
                "text": m.group(2).strip(),
                "citation": f"MANUAL:{os.path.basename(fp)}#{sec}@p.{m.group(1)}"}

    def resolve_citation(self, model: str, sec: str, page: Optional[int]) -> bool:
        """Does this exact citation resolve to real content? (groundedness gate)"""
        s = self.section(model, sec)
        if not s:
            return False
        return page is None or s["page"] == page


# --------------------------------------------------------------------------- #
class PartsAdapter:
    def __init__(self, dr: DataRoot) -> None:
        self.dr = dr
        with open(dr.path("parts", "inventory.csv"), encoding="utf-8") as f:
            self.rows = {r["part_no"]: r for r in csv.DictReader(f)}

    def lookup(self, part_no: str) -> Optional[dict]:
        return self.rows.get(part_no)

    def exists(self, part_no: str) -> bool:
        return part_no in self.rows


# --------------------------------------------------------------------------- #
class MOCAdapter:
    def __init__(self, dr: DataRoot) -> None:
        self.dr = dr

    def recent_changes(self, funcloc: str) -> list[dict]:
        out = []
        for fp in glob.glob(self.dr.path("moc", "*.json")):
            r = json.load(open(fp, encoding="utf-8"))
            if r.get("funcloc") == funcloc:
                out.append(r)
        return sorted(out, key=lambda r: r.get("date", ""), reverse=True)


# --------------------------------------------------------------------------- #
class SafetyAdapter:
    def __init__(self, dr: DataRoot) -> None:
        self.dr = dr

    def loto(self, funcloc: str) -> Optional[dict]:
        for cand in [f"LOTO-FILL-03.md" if "03-FILL" in funcloc else None,
                     f"LOTO-{funcloc}.md"]:
            if not cand:
                continue
            fp = self.dr.path("safety", cand)
            if os.path.exists(fp):
                txt = open(fp, encoding="utf-8").read()
                return {"procedure": cand, "text": txt,
                        "requires_permit": "permit" in txt.lower(),
                        "citation": f"SAFETY:{cand}"}
        return None


# --------------------------------------------------------------------------- #
class ShiftNotesAdapter:
    """Tribal knowledge in shift slang. Low trust, high lead value."""
    def __init__(self, dr: DataRoot) -> None:
        self.dr = dr

    def search(self, terms: list[str], limit: int = 5) -> list[dict]:
        hits = []
        for fp in sorted(glob.glob(self.dr.path("shift_notes", "*.md")), reverse=True):
            txt = open(fp, encoding="utf-8").read().lower()
            score = sum(1 for t in terms if t.lower() in txt)
            if score:
                hits.append({"file": os.path.basename(fp), "score": score,
                             "text": open(fp, encoding="utf-8").read().strip(),
                             "citation": f"SHIFT_NOTES:{os.path.basename(fp)}"})
        return sorted(hits, key=lambda h: -h["score"])[:limit]


# --------------------------------------------------------------------------- #
class SecurityScanner:
    """Air-gap / OT-security lens: flags planted defects (hardcoded creds, etc.)."""
    SECRET_RX = re.compile(r"(?i)(password|passwd|pwd|secret|token)\s*=\s*([^\s$].+)")

    def __init__(self, dr: DataRoot) -> None:
        self.dr = dr

    def scan_configs(self) -> list[dict]:
        findings = []
        for fp in glob.glob(self.dr.path("config", "*.ini")):
            for i, line in enumerate(open(fp, encoding="utf-8"), 1):
                m = self.SECRET_RX.search(line)
                if m and "${" not in line:
                    findings.append({"file": os.path.basename(fp), "line": i,
                                     "issue": "hardcoded_secret",
                                     "detail": m.group(1)})
                if "verify_ssl = false" in line.lower():
                    findings.append({"file": os.path.basename(fp), "line": i,
                                     "issue": "tls_verification_disabled",
                                     "detail": "verify_ssl=false"})
        return findings

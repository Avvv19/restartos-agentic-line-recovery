"""
restartos.audit
==============
Tamper-evident, hash-chained, append-only audit. "Logged" != defensible;
regulated plants need immutability. Every tool call, evidence item, decision,
approval (identity + rationale), artifact and outcome is chained:

    entry.hash = sha256(prev_hash + canonical_json(entry_body))

verify_chain() recomputes the chain and detects any tampering.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field


@dataclass
class AuditEntry:
    seq: int
    kind: str
    body: dict
    prev_hash: str
    ts: float = field(default_factory=time.time)
    hash: str = ""

    def compute(self) -> str:
        payload = json.dumps({"seq": self.seq, "kind": self.kind, "body": self.body,
                              "prev_hash": self.prev_hash, "ts": self.ts},
                             sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class AuditTrail:
    GENESIS = "0" * 64

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def append(self, kind: str, body: dict) -> AuditEntry:
        prev = self.entries[-1].hash if self.entries else self.GENESIS
        e = AuditEntry(seq=len(self.entries), kind=kind, body=body, prev_hash=prev)
        e.hash = e.compute()
        self.entries.append(e)
        return e

    def verify_chain(self) -> tuple[bool, str]:
        prev = self.GENESIS
        for e in self.entries:
            if e.prev_hash != prev:
                return False, f"broken link at seq {e.seq}"
            if e.compute() != e.hash:
                return False, f"tampered body at seq {e.seq}"
            prev = e.hash
        return True, "intact"

    def to_list(self) -> list[dict]:
        return [{"seq": e.seq, "kind": e.kind, "ts": e.ts, "hash": e.hash[:16],
                 "prev": e.prev_hash[:16], "body": e.body} for e in self.entries]

"""
restartos.llm.router
====================
The ModelRouter routes each call to a model + token budget chosen by PROBLEM
TYPE — "token usage maximized based on the problem/issue type":

  * FAST_PATH        -> cheapest/local model, tiny budget  (<60s preliminary cause)
  * EVIDENCE_LENS    -> cheap model, small budget          (specialist extraction)
  * DEEP_DIAGNOSIS   -> strongest model, large budget       (differential reasoning)
  * PLANNING         -> strong model, medium budget
  * VERIFICATION     -> DIFFERENT family from author, large budget (anti-collusion)
  * KNOWLEDGE        -> cheap model

It records every call into a ledger (tokens, cost, latency) and enforces a
per-incident budget (count / cost / wall-clock) used by the orchestrator's
bounded retry loop.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import yaml  # type: ignore

from .providers import Completion, get_provider


class ProblemType(str, Enum):
    FAST_PATH = "FAST_PATH"
    EVIDENCE_LENS = "EVIDENCE_LENS"
    DEEP_DIAGNOSIS = "DEEP_DIAGNOSIS"
    PLANNING = "PLANNING"
    VERIFICATION = "VERIFICATION"
    KNOWLEDGE = "KNOWLEDGE"


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    problem_type: ProblemType
    tokens: int
    cost_usd: float
    latency_s: float


@dataclass
class CallRecord:
    problem_type: str
    provider: str
    model: str
    tokens: int
    cost_usd: float
    latency_s: float


@dataclass
class Budget:
    max_calls: int = 40
    max_cost_usd: float = 2.50
    max_wall_clock_s: float = 120.0
    started: float = field(default_factory=time.time)
    calls: int = 0
    cost: float = 0.0

    def exhausted(self) -> tuple[bool, str]:
        if self.calls >= self.max_calls:
            return True, f"call budget {self.max_calls} reached"
        if self.cost >= self.max_cost_usd:
            return True, f"cost budget ${self.max_cost_usd} reached"
        if time.time() - self.started >= self.max_wall_clock_s:
            return True, f"wall-clock {self.max_wall_clock_s}s reached"
        return False, ""


# Default routing table (severity escalates DEEP_DIAGNOSIS to the strongest tier)
DEFAULT_ROUTING = {
    "FAST_PATH":      {"providers": ["groq", "gemini", "ollama", "anthropic", "mock"], "model": {"groq": "llama-3.3-70b-versatile", "gemini": "gemini-2.0-flash", "ollama": "llama3.1:8b", "anthropic": "claude-haiku-4-5", "mock": "mock-fast"},   "max_tokens": 512,  "temperature": 0.0},
    "EVIDENCE_LENS":  {"providers": ["groq", "gemini", "ollama", "anthropic", "mock"], "model": {"groq": "llama-3.3-70b-versatile", "gemini": "gemini-2.0-flash", "ollama": "llama3.1:8b", "anthropic": "claude-haiku-4-5", "mock": "mock-lens"},   "max_tokens": 1024, "temperature": 0.0},
    "DEEP_DIAGNOSIS": {"providers": ["nim", "anthropic", "openai", "mock"], "model": {"nim": "nvidia/nemotron-3-ultra-550b-a55b", "anthropic": "claude-opus-4-8", "openai": "gpt-4o", "mock": "mock-deep"},          "max_tokens": 4096, "temperature": 0.0},
    "PLANNING":       {"providers": ["nim", "anthropic", "openai", "mock"], "model": {"nim": "nvidia/nemotron-3-ultra-550b-a55b", "anthropic": "claude-sonnet-4-6", "openai": "gpt-4o", "mock": "mock-plan"},        "max_tokens": 3072, "temperature": 0.0},
    "VERIFICATION":   {"providers": ["groq", "gemini", "openai", "anthropic", "mock"], "model": {"groq": "llama-3.3-70b-versatile", "gemini": "gemini-2.0-flash", "openai": "gpt-4o", "anthropic": "claude-sonnet-4-6", "mock": "mock-verify"},      "max_tokens": 3072, "temperature": 0.0},
    "KNOWLEDGE":      {"providers": ["gemini", "groq", "ollama", "anthropic", "mock"], "model": {"gemini": "gemini-2.0-flash", "groq": "llama-3.3-70b-versatile", "ollama": "llama3.1:8b", "anthropic": "claude-haiku-4-5", "mock": "mock-know"},    "max_tokens": 1024, "temperature": 0.0},
}


class ModelRouter:
    def __init__(self, routing: Optional[dict] = None, budget: Optional[Budget] = None,
                 config_path: Optional[str] = None) -> None:
        self.routing = routing or self._load(config_path) or DEFAULT_ROUTING
        self.budget = budget or Budget()
        self.ledger: list[CallRecord] = []
        self._author_provider: Optional[str] = None  # set on first author call

    @staticmethod
    def _load(path: Optional[str]) -> Optional[dict]:
        if path and os.path.exists(path):
            with open(path) as f:
                return yaml.safe_load(f).get("routing")
        return None

    def _pick(self, pt: ProblemType) -> tuple[str, str, dict]:
        spec = self.routing[pt.value]
        for pname in spec["providers"]:
            prov = get_provider(pname)
            if prov.available():
                # Anti-collusion: never let the verifier resolve to the author's
                # exact provider. Fall through to the next candidate if so.
                if pt == ProblemType.VERIFICATION and pname == self._author_provider \
                        and len(spec["providers"]) > 1:
                    continue
                model = spec["model"].get(pname, "default")
                return pname, model, spec
        # last resort
        return "mock", spec["model"].get("mock", "mock"), spec

    def complete(self, problem_type: ProblemType, *, system: str, prompt: str,
                 author: bool = False) -> LLMResponse:
        done, why = self.budget.exhausted()
        if done:
            raise BudgetExceeded(why)
        pname, model, spec = self._pick(problem_type)
        if author:
            self._author_provider = pname
        prov = get_provider(pname)
        t0 = time.time()
        try:
            c: Completion = prov.complete(model=model, system=system, prompt=prompt,
                                          temperature=spec["temperature"],
                                          max_tokens=spec["max_tokens"])
        except Exception:
            # graceful degradation to mock keeps the pipeline runnable
            c = get_provider("mock").complete(model="mock", system=system,
                                              prompt=prompt, temperature=0.0,
                                              max_tokens=spec["max_tokens"])
            pname = "mock"
        dt = time.time() - t0
        self.budget.calls += 1
        self.budget.cost += c.cost_usd
        self.ledger.append(CallRecord(problem_type.value, pname, c.model,
                                      c.total_tokens, c.cost_usd, round(dt, 4)))
        return LLMResponse(c.text, c.model, pname, problem_type,
                           c.total_tokens, c.cost_usd, round(dt, 4))

    def usage_summary(self) -> dict:
        by_type: dict[str, dict] = {}
        for r in self.ledger:
            d = by_type.setdefault(r.problem_type, {"calls": 0, "tokens": 0, "cost": 0.0})
            d["calls"] += 1
            d["tokens"] += r.tokens
            d["cost"] = round(d["cost"] + r.cost_usd, 6)
        return {
            "total_calls": self.budget.calls,
            "total_tokens": sum(r.tokens for r in self.ledger),
            "total_cost_usd": round(self.budget.cost, 6),
            "by_problem_type": by_type,
            "providers_used": sorted({r.provider for r in self.ledger}),
        }


class BudgetExceeded(Exception):
    pass

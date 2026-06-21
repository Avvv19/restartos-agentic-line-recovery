"""
restartos.llm.providers
=======================
Provider abstraction over hosted APIs (Anthropic, OpenAI), local/open models
(Ollama), and a deterministic MockProvider so the whole pipeline runs end-to-end
with ZERO keys. Real providers activate automatically when their SDK + key exist.

Anti-collusion is enforced at the router layer: the verifier must run on a
*different* provider/model family than the author. The provider layer only needs
to expose a uniform .complete() returning text + token accounting.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class Completion:
    text: str
    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class BaseProvider:
    name = "base"

    def available(self) -> bool:
        return False

    def complete(self, *, model: str, system: str, prompt: str,
                 temperature: float, max_tokens: int,
                 response_schema: Optional[dict] = None) -> Completion:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Deterministic mock — the default. Produces grounded, schema-shaped output by  #
# reading hints embedded in the prompt. Temperature 0 => identical output for   #
# identical (fault, evidence) — exactly what manufacturing demands.             #
# --------------------------------------------------------------------------- #
class MockProvider(BaseProvider):
    name = "mock"

    def available(self) -> bool:
        return True

    def complete(self, *, model, system, prompt, temperature, max_tokens,
                 response_schema=None) -> Completion:
        # If the caller asked for JSON and embedded a __MOCK_JSON__ block, echo it.
        # This lets agents stay declarative: they pass the structured answer they
        # derived deterministically from real dataset rows, and the "LLM" returns
        # it verbatim. Real providers would synthesize this from the same context.
        m = re.search(r"__MOCK_JSON__(.*?)__END_MOCK_JSON__", prompt, re.S)
        if m:
            text = m.group(1).strip()
        else:
            text = self._summarize(prompt)
        ptoks = max(1, len(system + prompt) // 4)
        ctoks = max(1, len(text) // 4)
        return Completion(text=text, model=model, provider=self.name,
                          prompt_tokens=ptoks, completion_tokens=ctoks, cost_usd=0.0)

    @staticmethod
    def _summarize(prompt: str) -> str:
        h = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        return json.dumps({"note": "mock-deterministic", "digest": h})


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    # rough public list prices ($/1M tokens) for cost accounting
    PRICES = {
        "claude-opus-4-8":   (15.0, 75.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5":  (0.80, 4.0),
    }

    def __init__(self) -> None:
        self._client = None

    def available(self) -> bool:
        return bool(os.getenv("ANTHROPIC_API_KEY"))

    def _client_or_none(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic  # type: ignore
            self._client = anthropic.Anthropic()
            return self._client
        except Exception:
            return None

    def complete(self, *, model, system, prompt, temperature, max_tokens,
                 response_schema=None) -> Completion:
        client = self._client_or_none()
        if client is None:
            raise RuntimeError("anthropic SDK/key unavailable")
        msg = client.messages.create(
            model=model, system=system, max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in msg.content)
        pin, pout = self.PRICES.get(model, (3.0, 15.0))
        pt, ct = msg.usage.input_tokens, msg.usage.output_tokens
        cost = (pt * pin + ct * pout) / 1_000_000
        return Completion(text, model, self.name, pt, ct, round(cost, 6))


class OpenAIProvider(BaseProvider):
    name = "openai"
    PRICES = {"gpt-4o": (2.5, 10.0), "gpt-4o-mini": (0.15, 0.60), "o4-mini": (1.1, 4.4)}

    def __init__(self) -> None:
        self._client = None

    def available(self) -> bool:
        return bool(os.getenv("OPENAI_API_KEY"))

    def complete(self, *, model, system, prompt, temperature, max_tokens,
                 response_schema=None) -> Completion:
        try:
            from openai import OpenAI  # type: ignore
        except Exception as e:
            raise RuntimeError(f"openai SDK unavailable: {e}")
        client = self._client or OpenAI()
        self._client = client
        resp = client.chat.completions.create(
            model=model, temperature=temperature, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": prompt}],
        )
        text = resp.choices[0].message.content or ""
        u = resp.usage
        pin, pout = self.PRICES.get(model, (1.0, 3.0))
        cost = (u.prompt_tokens * pin + u.completion_tokens * pout) / 1_000_000
        return Completion(text, model, self.name, u.prompt_tokens,
                          u.completion_tokens, round(cost, 6))


class OllamaProvider(BaseProvider):
    """Local/open models. Free (self-hosted) -> cost 0. Good for cheap lenses."""
    name = "ollama"

    def available(self) -> bool:
        if os.getenv("RESTARTOS_ENABLE_OLLAMA") != "1":
            return False
        try:
            import urllib.request
            host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            urllib.request.urlopen(host + "/api/tags", timeout=0.5)
            return True
        except Exception:
            return False

    def complete(self, *, model, system, prompt, temperature, max_tokens,
                 response_schema=None) -> Completion:
        import urllib.request
        host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        body = json.dumps({"model": model, "system": system, "prompt": prompt,
                           "stream": False,
                           "options": {"temperature": temperature}}).encode()
        req = urllib.request.Request(host + "/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        text = data.get("response", "")
        pt = data.get("prompt_eval_count", len(prompt) // 4)
        ct = data.get("eval_count", len(text) // 4)
        return Completion(text, model, self.name, pt, ct, 0.0)



class OpenAICompatProvider(BaseProvider):
    """OpenAI-compatible endpoints (NVIDIA NIM, Groq, Gemini OpenAI mode, etc.),
    configured entirely by environment so keys never live in source.
      <PREFIX>_API_KEY (required), <PREFIX>_BASE_URL, <PREFIX>_MODEL
    """
    def __init__(self, name: str, prefix: str, default_base: str, default_model: str) -> None:
        self.name = name
        self.prefix = prefix
        self.default_base = default_base
        self.default_model = default_model
        self._client = None

    def _key(self):
        return os.getenv(self.prefix + "_API_KEY")

    def available(self) -> bool:
        return bool(self._key())

    @property
    def model(self) -> str:
        return os.getenv(self.prefix + "_MODEL", self.default_model)

    def _client_or_none(self):
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI  # type: ignore
            self._client = OpenAI(api_key=self._key(),
                                  base_url=os.getenv(self.prefix + "_BASE_URL", self.default_base))
            return self._client
        except Exception:
            return None

    def complete(self, *, model, system, prompt, temperature, max_tokens,
                 response_schema=None) -> Completion:
        try:
            from openai import OpenAI  # type: ignore
            client = self._client or OpenAI(api_key=self._key(),
                base_url=os.getenv(self.prefix + "_BASE_URL", self.default_base))
            self._client = client
        except Exception as e:
            raise RuntimeError(f"{self.name}: client init failed: {e}")
        use_model = self.model  # provider-specific model id from env
        resp = client.chat.completions.create(
            model=use_model, temperature=temperature, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": prompt}])
        text = resp.choices[0].message.content or ""
        u = getattr(resp, "usage", None)
        pt = getattr(u, "prompt_tokens", len(prompt) // 4) if u else len(prompt) // 4
        ct = getattr(u, "completion_tokens", len(text) // 4) if u else len(text) // 4
        return Completion(text, use_model, self.name, pt, ct, 0.0)


_REGISTRY = {p.name: p for p in
             [MockProvider(), AnthropicProvider(), OpenAIProvider(), OllamaProvider(),
              OpenAICompatProvider("nim", "NIM", "https://integrate.api.nvidia.com/v1",
                                   "nvidia/nemotron-3-ultra-550b-a55b"),
              OpenAICompatProvider("groq", "GROQ", "https://api.groq.com/openai/v1",
                                   "llama-3.3-70b-versatile"),
              OpenAICompatProvider("gemini", "GEMINI",
                                   "https://generativelanguage.googleapis.com/v1beta/openai",
                                   "gemini-2.0-flash")]}


def get_provider(name: str) -> BaseProvider:
    return _REGISTRY.get(name, _REGISTRY["mock"])

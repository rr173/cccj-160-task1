"""Deterministic model provider.

Real LLM calls are non-deterministic and need network access; for a governance
service we need *reproducible* completions whose provenance is captured by a
provider fingerprint. The fake provider:

* renders the exact prompt the online layer would send
* derives output deterministically from (model fingerprint, seed, params,
  rendered prompt, inputs)
* the fingerprint changes when the provider implementation/revision changes,
  and can be rotated at runtime to simulate a silent upstream model revision
  (same model_id, different weights) -> replay flags MODEL axis.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field

from . import canonical

# Registry of declared model identifiers and their base revision.
MODELS: dict[str, dict] = {
    "llama-3.1-8b": {"provider": "acme-llm", "revision": "r2024-09-01"},
    "gpt-style-4o": {"provider": "acme-llm", "revision": "r2024-09-15"},
}


def render_prompt(system_instructions: str, variables: dict) -> str:
    """Render system instructions with variables.

    Substitution uses {{name}} tokens; unknown tokens are left for contract
    validation to catch (caller validates required variables first).
    """
    prompt = system_instructions
    for name, value in sorted(variables.items()):
        prompt = prompt.replace("{{" + name + "}}", str(value))
    return prompt


@dataclass
class ModelResponse:
    text: str
    model_id: str
    revision: str
    fingerprint: str
    prompt_sha: str
    deterministic: bool
    trace: dict = field(default_factory=dict)


class ModelProvider:
    """Thread-safe deterministic provider with a runtime-rotatable fingerprint."""

    def __init__(self, models: dict | None = None):
        self._models = dict(models or MODELS)
        # deployment_rev models (model_id -> override revision), simulating an
        # upstream silent weights rollout keeping the same advertised model id.
        self._override_rev: dict[str, str] = {}
        self._lock = threading.RLock()

    def register_model(self, model_id: str, provider: str, revision: str) -> None:
        with self._lock:
            self._models[model_id] = {"provider": provider, "revision": revision}
            self._override_rev.pop(model_id, None)

    def rotate(self, model_id: str, new_revision: str) -> str:
        """Simulate silent upstream revision bump; returns new fingerprint."""
        with self._lock:
            if model_id not in self._models:
                raise KeyError(f"unknown model {model_id}")
            self._override_rev[model_id] = new_revision
            return self.fingerprint(model_id)

    def revision(self, model_id: str) -> str:
        with self._lock:
            if model_id not in self._models:
                raise KeyError(f"unknown model {model_id}")
            return self._override_rev.get(model_id, self._models[model_id]["revision"])

    def fingerprint(self, model_id: str) -> str:
        with self._lock:
            meta = self._models[model_id]
            rev = self.revision(model_id)
            return canonical.sha256_hex(
                {
                    "spec": canonical.SPEC_VERSION,
                    "kind": "model-provider",
                    "model_id": model_id,
                    "provider": meta["provider"],
                    "revision": rev,
                }
            )[:16]

    def list_models(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "model_id": mid,
                    "provider": meta["provider"],
                    "advertised_revision": meta["revision"],
                    "active_revision": self.revision(mid),
                    "fingerprint": self.fingerprint(mid),
                }
                for mid, meta in sorted(self._models.items())
            ]

    def complete(
        self,
        *,
        model_id: str,
        system_instructions: str,
        variables: dict,
        model_params: dict,
    ) -> ModelResponse:
        with self._lock:
            if model_id not in self._models:
                raise KeyError(f"unknown model {model_id}")
            provider = self._models[model_id]["provider"]
            revision = self.revision(model_id)
            fp = self.fingerprint(model_id)

        rendered = render_prompt(system_instructions, variables)
        prompt_sha = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]

        basis = {
            "model_id": model_id,
            "provider": provider,
            "revision": revision,
            "prompt": rendered,
            "variables": variables,
            "params": {k: v for k, v in sorted(model_params.items())},
        }
        h = hashlib.sha256(canonical.dumps(basis).encode("utf-8")).digest()
        n = int.from_bytes(h[:8], "big")

        seed = model_params.get("seed")
        if isinstance(seed, int):
            n = (n + seed) % (2**64)

        token_a = h[8:12].hex()
        token_b = h[12:16].hex()
        answer_kind = n % 4
        primary = variables.get("question") or variables.get("input") or variables.get("q") or ""
        if answer_kind == 0:
            text = f"yes[{token_a}/{token_b}]"
        elif answer_kind == 1:
            text = f"no[{token_a}/{token_b}]"
        elif answer_kind == 2:
            text = f"maybe: {str(primary)[:40]} [{token_a}]"
        else:
            text = f"answer-{n % 1000} [{token_a}{token_b}]"

        return ModelResponse(
            text=text,
            model_id=model_id,
            revision=revision,
            fingerprint=fp,
            prompt_sha=prompt_sha,
            deterministic=True,
            trace={
                "provider": provider,
                "basis_sha": h.hex()[:16],
                "answer_kind": answer_kind,
            },
        )

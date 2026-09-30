"""Canonical serialization and content hashing.

Every hashable object in the system is reduced to a canonical form before
hashing: JSON with sorted keys, no insignificant whitespace, Unicode text.
This makes hashes stable across processes and independent of dict order.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

SPEC_VERSION = "gov-v1"


def dumps(obj: Any) -> str:
    """Canonical JSON text: sorted keys, separators without spaces."""
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(dumps(obj).encode("utf-8")).hexdigest()


def fingerprint(obj: Any, *, prefix: str = "sha256") -> str:
    return f"{prefix}:{sha256_hex(obj)[:16]}"


# --- Template body normalization ------------------------------------------

def normalize_tools(tools: Any) -> list[str]:
    """Tool permissions are a sorted list of flat grant strings.

    Grant grammar (enforced in governance): "tool.action", e.g.
    "search.query", "calendar.event.create".
    """
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise ValueError("tools must be a list of grant strings")
    out = []
    for t in tools:
        if not isinstance(t, str) or not t.strip():
            raise ValueError("each tool grant must be a non-empty string")
        out.append(t.strip())
    return sorted(set(out))


def normalize_model_params(params: Any) -> dict:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValueError("model_params must be an object")
    clean: dict[str, Any] = {}
    allowed = {"temperature", "top_p", "max_tokens", "stop", "seed", "extra"}
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"unknown model params: {sorted(unknown)}")
    if "temperature" in params:
        t = params["temperature"]
        if not isinstance(t, (int, float)) or not (0 <= float(t) <= 2):
            raise ValueError("temperature must be a number in [0,2]")
        clean["temperature"] = float(t)
    if "top_p" in params:
        p = params["top_p"]
        if not isinstance(p, (int, float)) or not (0 < float(p) <= 1):
            raise ValueError("top_p must be a number in (0,1]")
        clean["top_p"] = float(p)
    if "max_tokens" in params:
        m = params["max_tokens"]
        if not isinstance(m, int) or m <= 0:
            raise ValueError("max_tokens must be a positive integer")
        clean["max_tokens"] = m
    if "stop" in params:
        stop = params["stop"]
        if not isinstance(stop, (str, list)):
            raise ValueError("stop must be a string or list")
        clean["stop"] = stop
    if "seed" in params and params["seed"] is not None:
        if not isinstance(params["seed"], int):
            raise ValueError("seed must be an integer")
        clean["seed"] = params["seed"]
    if "extra" in params:
        if not isinstance(params["extra"], dict):
            raise ValueError("model_params.extra must be an object")
        clean["extra"] = params["extra"]
    return clean


def normalize_variables(variables: Any) -> dict[str, dict]:
    """Normalize the variable contract.

    Each variable: {name: {type, required, default?, enum?, description?}}.
    Canonical contract stored as a sorted-key mapping.
    """
    if variables is None:
        return {}
    if not isinstance(variables, dict):
        raise ValueError("variables must be an object name -> spec")
    out: dict[str, dict] = {}
    valid_types = {"string", "number", "integer", "boolean"}
    for name, spec in variables.items():
        if not isinstance(name, str) or not name:
            raise ValueError("variable name must be a non-empty string")
        if not isinstance(spec, dict):
            raise ValueError(f"variable {name!r} spec must be an object")
        vtype = spec.get("type", "string")
        if vtype not in valid_types:
            raise ValueError(f"variable {name!r}: unsupported type {vtype!r}")
        item: dict[str, Any] = {
            "type": vtype,
            "required": bool(spec.get("required", False)),
        }
        if "description" in spec:
            item["description"] = str(spec["description"])
        if "enum" in spec and spec["enum"] is not None:
            enum = spec["enum"]
            if not isinstance(enum, list) or not enum:
                raise ValueError(f"variable {name!r}: enum must be a non-empty list")
            item["enum"] = sorted(enum, key=lambda x: dumps(x))
        if "default" in spec and spec["default"] is not None:
            item["default"] = spec["default"]
        if item.get("required") and "default" in item:
            raise ValueError(f"variable {name!r}: required variable cannot have default")
        out[name] = item
    return out


def normalize_body(body: dict) -> dict:
    """Return the canonical, hashable template body (the four components)."""
    if not isinstance(body, dict):
        raise ValueError("template body must be an object")
    instructions = body.get("system_instructions", "")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("system_instructions must be a non-empty string")
    model_id = body.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id is required")
    return {
        "system_instructions": instructions,
        "variables": normalize_variables(body.get("variables")),
        "tools": normalize_tools(body.get("tools")),
        "model_params": normalize_model_params(body.get("model_params")),
        "model_id": model_id.strip(),
    }


def content_hash(body: dict) -> str:
    return sha256_hex({"spec": SPEC_VERSION, "kind": "template-body", "body": body})


def version_hash(template_id: str, semver: str, chash: str, created_by: str) -> str:
    return sha256_hex(
        {
            "spec": SPEC_VERSION,
            "kind": "template-version",
            "template_id": template_id,
            "semver": semver,
            "content_hash": chash,
            "created_by": created_by,
        }
    )


def input_digest(variables: dict) -> str:
    return sha256_hex({"spec": SPEC_VERSION, "kind": "request-input", "variables": variables})


def grants_snapshot_hash(grants: list[str]) -> str:
    return sha256_hex({"spec": SPEC_VERSION, "kind": "grants-snapshot", "grants": grants})


def evidence_hash(rows: list[dict], summary: dict, testset_hash: str) -> str:
    """Tamper-evident hash over per-case results + aggregate + frozen set."""
    return sha256_hex(
        {
            "spec": SPEC_VERSION,
            "kind": "evaluation-evidence",
            "testset_hash": testset_hash,
            "summary": summary,
            "rows": rows,
        }
    )

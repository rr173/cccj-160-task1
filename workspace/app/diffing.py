"""Structural diffing of template bodies and change classification.

Used for:
* semver bump suggestion when creating a new version
* deciding whether tool permissions were expanded (approval gate)
* replay reports explaining *what* changed between two versions
"""
from __future__ import annotations


def _contract_index(contract: dict) -> dict[str, dict]:
    return contract or {}


def contract_changes(old: dict, new: dict) -> dict:
    old, new = _contract_index(old), _contract_index(new)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = []
    for name in sorted(set(old) & set(new)):
        a, b = old[name], new[name]
        delta = {}
        for key in ("type", "required", "enum", "default", "description"):
            if a.get(key) != b.get(key):
                delta[key] = {"from": a.get(key), "to": b.get(key)}
        if delta:
            changed.append({"variable": name, "fields": delta})
    return {"added": added, "removed": removed, "changed": changed}


def contract_is_breaking(ch: dict) -> bool:
    if ch["removed"]:
        return True
    for c in ch["changed"]:
        f = c["fields"]
        if "type" in f:
            return True
        if "required" in f and f["required"]["to"] is True:
            return True
        if "enum" in f:
            old_enum = f["enum"]["from"] or []
            new_enum = f["enum"]["to"] or []
            if set(map(str, old_enum)) - set(map(str, new_enum)):
                return True  # values removed
        if "default" in f and f["default"]["to"] is None and f["default"]["from"] is not None:
            return True
    # A newly added *required* variable breaks callers that don't supply it.
    for name in ch["added"]:
        if name and False:
            pass
    return False


def added_contract_requirements(new_names: list[str], new_contract: dict) -> list[str]:
    return [n for n in new_names if new_contract.get(n, {}).get("required")]


def tool_changes(old_tools: list[str], new_tools: list[str]) -> dict:
    old_set, new_set = set(old_tools or []), set(new_tools or [])
    return {
        "added": sorted(new_set - old_set),
        "removed": sorted(old_set - new_set),
        "expanded": bool(new_set - old_set),
        "reduced": bool(old_set - new_set),
    }


def param_changes(old_p: dict, new_p: dict) -> dict:
    keys = sorted(set((old_p or {})) | set((new_p or {})))
    changed = []
    for k in keys:
        if (old_p or {}).get(k) != (new_p or {}).get(k):
            changed.append({"param": k, "from": (old_p or {}).get(k), "to": (new_p or {}).get(k)})
    return changed


def body_diff(old: dict | None, new: dict) -> dict:
    """Return field-level diff between canonical bodies. old=None => initial."""
    if old is None:
        return {"initial": True}
    diff: dict = {"initial": False, "changed_fields": []}
    if old["system_instructions"] != new["system_instructions"]:
        diff["changed_fields"].append("system_instructions")
        diff["system_instructions"] = {
            "from": old["system_instructions"],
            "to": new["system_instructions"],
        }
    if old.get("model_id") != new.get("model_id"):
        diff["changed_fields"].append("model_id")
        diff["model_id"] = {"from": old.get("model_id"), "to": new.get("model_id")}

    cc = contract_changes(old["variables"], new["variables"])
    if cc["added"] or cc["removed"] or cc["changed"]:
        diff["changed_fields"].append("variables")
        diff["variables"] = cc

    tc = tool_changes(old["tools"], new["tools"])
    if tc["added"] or tc["removed"]:
        diff["changed_fields"].append("tools")
        diff["tools"] = tc

    pc = param_changes(old["model_params"], new["model_params"])
    if pc:
        diff["changed_fields"].append("model_params")
        diff["model_params"] = pc
    return diff


def classify_change(old: dict | None, new: dict) -> dict:
    """Classify into major/minor/patch and describe the bump basis.

    Rules:
      major   : contract breakage, model_id switch, or tool grant *expansion*
                (expansion also triggers mandatory separate approval)
      minor   : backwards-compatible contract additions/relaxations,
                tool grant reduction, model_param tuning
      patch   : system instruction text only
      initial : first version
    """
    if old is None:
        return {"class": "initial", "reasons": ["first version"], "diff": {"initial": True}}

    diff = body_diff(old, new)
    reasons: list[str] = []
    level = 0  # 0=patch,1=minor,2=major

    if "system_instructions" in diff["changed_fields"]:
        reasons.append("system instructions updated")
    if "model_id" in diff["changed_fields"]:
        reasons.append("model identifier switched")
        level = max(level, 2)
    if "variables" in diff["changed_fields"]:
        cc = diff["variables"]
        if contract_is_breaking(cc) or added_contract_requirements(cc["added"], new["variables"]):
            reasons.append("backwards-incompatible variable contract change")
            level = 2
        else:
            reasons.append("backwards-compatible variable contract change")
            level = max(level, 1)
    if "tools" in diff["changed_fields"]:
        tc = diff["tools"]
        if tc["expanded"]:
            reasons.append(f"tool permissions expanded: {tc['added']}")
            level = 2
        if tc["reduced"]:
            reasons.append(f"tool permissions reduced: {tc['removed']}")
            level = max(level, 1)
    if "model_params" in diff["changed_fields"]:
        reasons.append("model parameters changed")
        level = max(level, 1)

    label = {0: "patch", 1: "minor", 2: "major"}[level]
    return {"class": label, "reasons": reasons, "diff": diff}


def next_semver(prev: str | None, klass: str) -> str:
    if prev is None:
        return "1.0.0"
    major, minor, patch = (int(x) for x in prev.split("."))
    if klass == "major":
        return f"{major + 1}.0.0"
    if klass == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"

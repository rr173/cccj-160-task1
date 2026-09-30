#!/usr/bin/env python3
"""End-to-end API demonstration of the governance & reproducibility service.

Walks through:
  1. template + v1, fixed test set, evaluation evidence, approval, publish
  2. online inference bound to exact version/model/input fingerprints
  3. replay: exact reproduction
  4. hot update (v2 with tool-permission expansion blocked, then approved)
  5. replay distinguishing INPUT / MODEL / TEMPLATE axes
  6. late evaluation evidence never overwrites the pinned publish snapshot
  7. concurrent rollback/update: only one complete version ever serves
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def step(n, title):
    print(f"\n{'='*72}\nSTEP {n}: {title}\n{'='*72}")


def show(label, status, obj, keys=()):
    mark = "OK " if status < 400 else f"{status}"
    print(f"[{mark}] {label}")
    if keys:
        for k in keys:
            cur = obj
            for part in k.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is not None:
                print(f"      {k} = {cur}")
    return obj


body_v1 = {
    "system_instructions": "You are a support agent for {{product}}. Answer briefly. Q: {{question}}",
    "variables": {
        "product": {"type": "string", "required": True, "enum": ["billing", "cloud"]},
        "question": {"type": "string", "required": True},
    },
    "tools": ["kb.search"],
    "model_params": {"temperature": 0.2, "seed": 7, "max_tokens": 256},
    "model_id": "llama-3.1-8b",
}
cases = [
    {"case_id": "refund", "inputs": {"product": "billing", "question": "refund?"},
     "expect": {"min_len": 3}},
    {"case_id": "cloud", "inputs": {"product": "cloud", "question": "regions?"},
     "expect": {"min_len": 3}},
]

# 1 -------------------------------------------------------------------------
step(1, "创建模板、v1、固定测试集、评估、审批、发布")
s, tpl = call("POST", "/api/templates", {"name": "customer-support", "actor": "alice",
                                         "description": "客服提示词"})
show("create template", s, tpl, ["template_id"]); tid = tpl["template_id"]

s, v1 = call("POST", f"/api/templates/{tid}/versions", {**body_v1, "actor": "alice"})
show("create v1", s, v1, ["semver", "change_class", "version_hash", "content_hash"]); v1h = v1["version_hash"]

s, ts = call("POST", "/api/testsets", {"name": "golden-v1", "cases": cases, "actor": "qa"})
show("create testset", s, ts, ["testset_id", "content_hash"]); tsid = ts["testset_id"]
s, _ = call("POST", f"/api/testsets/{tsid}/freeze", {})
print(f"[{s}] frozen testset {tsid}")

s, ev = call("POST", f"/api/versions/{v1h}/evaluate", {"testset_id": tsid, "actor": "ci",
                                                        "background": False})
show("evaluation v1", s, ev, ["status", "summary.pass_rate", "evidence_hash", "testset_hash", "late"])

s, apr = call("POST", f"/api/versions/{v1h}/approvals", {"kind": "publish", "actor": "qa-lead"})
show("request publish approval", s, apr, ["approval_id", "status"]); aid = apr["approval_id"]
s, _ = call("POST", f"/api/approvals/{aid}/decision", {"approve": True, "actor": "release-board"})
print(f"[{s}] approval decided")

s, pub = call("POST", f"/api/versions/{v1h}/publish", {"actor": "release-manager"})
show("publish v1 (gated)", s, pub, ["status", "semver", "evidence_hash"])
s, _ = call("POST", f"/api/templates/{tid}/activate",
            {"version_hash": v1h, "actor": "ops", "reason": "initial rollout"})
print(f"[{s}] v1 activated as single live pointer")

# 2 -------------------------------------------------------------------------
step(2, "线上请求绑定精确版本/模型/输入摘要")
s, req = call("POST", f"/api/templates/{tid}/infer",
              {"inputs": {"product": "billing", "question": "how do I get a refund?"}})
show("infer #1", s, req, ["request_id", "binding.semver", "binding.version_hash",
                          "binding.model_fingerprint", "binding.input_digest", "binding.prompt_sha"])
rid = req["request_id"]
print(f"      output = {req['output']!r}")

# 3 -------------------------------------------------------------------------
step(3, "重演：归档版本+归档输入 → 精确复现")
s, rep = call("POST", f"/api/requests/{rid}/replay", {"mode": "recorded"})
show("replay recorded", s, rep, ["verdict", "reproduced", "axes_changed", "relevant_axes_changed"])

# 4 -------------------------------------------------------------------------
step(4, "v2 扩大工具权限：必须重新审批，否则拒绝发布")
body_v2 = {**body_v1,
           "system_instructions": body_v1["system_instructions"] + " You may issue refunds.",
           "tools": ["kb.search", "billing.refund"]}
s, v2 = call("POST", f"/api/templates/{tid}/versions", {**body_v2, "actor": "bob"})
show("create v2", s, v2, ["semver", "change_class"]); v2h = v2["version_hash"]
bump = v2["bump_basis"]
print("      reasons:", bump["reasons"])

s, ev2 = call("POST", f"/api/versions/{v2h}/evaluate", {"testset_id": tsid, "background": False})
s, apr2 = call("POST", f"/api/versions/{v2h}/approvals", {"kind": "publish", "actor": "bob"})
s, _ = call("POST", f"/api/approvals/{apr2['approval_id']}/decision",
            {"approve": True, "actor": "release-board"})
s, blocked = call("POST", f"/api/versions/{v2h}/publish", {"actor": "release-manager"})
show("publish WITHOUT expansion approval (must be 403)", s, blocked, ["error", "message"])

s, expa = call("POST", f"/api/versions/{v2h}/approvals",
               {"kind": "permission_expansion", "actor": "bob", "reason": "refunds workflow"})
show("request expansion approval", s, expa,
     ["grants_expansion.added", "grants_snapshot_hash"]); exid = expa["approval_id"]
s, _ = call("POST", f"/api/approvals/{exid}/decision",
            {"approve": True, "actor": "security-board", "note": "scoped to billing"})
s, pub2 = call("POST", f"/api/versions/{v2h}/publish", {"actor": "release-manager"})
show("publish v2 after re-approval", s, pub2, ["status", "evidence_hash"])
s, _ = call("POST", f"/api/templates/{tid}/activate",
            {"version_hash": v2h, "actor": "ops", "reason": "hot update", "expected_active": v1h})
print(f"[{s}] hot update v1 -> v2 (atomic pointer swap)")

# 5 -------------------------------------------------------------------------
step(5, "重演三轴归因：INPUT / MODEL / TEMPLATE")
s, r_in = call("POST", f"/api/requests/{rid}/replay",
               {"mode": "recorded",
                "inputs": {"product": "cloud", "question": "where are my machines?"}})
show("INPUT axis", s, r_in, ["verdict", "axes_changed", "relevant_axes_changed"])

s, r_tpl = call("POST", f"/api/requests/{rid}/replay", {"mode": "active"})
show("TEMPLATE axis (active mode runs v2)", s, r_tpl,
     ["verdict", "axes_changed", "replay.fingerprints.version_hash"])

s, rot = call("POST", "/api/models/rotate", {"model_id": "llama-3.1-8b",
                                             "revision": "r2024-10-02-silent"})
show("simulate silent upstream model revision", s, rot, ["fingerprint"])
s, r_mod = call("POST", f"/api/requests/{rid}/replay", {"mode": "recorded"})
show("MODEL axis (same model_id, new fingerprint)", s, r_mod,
     ["verdict", "axes_changed", "axes.MODEL.detail.current_revision"])

# 6 -------------------------------------------------------------------------
step(6, "迟到评估：不覆盖发布时钉住的证据快照")
s, late = call("POST", f"/api/versions/{v1h}/evaluate",
               {"testset_id": tsid, "delay": 0, "background": False})
show("evaluation on already-published v1", s, late, ["status", "late", "publish_ref"])
s, v1again = call("GET", f"/api/versions/{v1h}")
pinned_ok = v1again["evidence_hash"] == pub["evidence_hash"]
print(f"[--] v1 pinned evidence_hash unchanged by late run: {pinned_ok}")
print(f"     pinned = {pub['evidence_hash'][:24]}…")
print(f"     late.publish_ref = {late['publish_ref'][:24]}…")

# 7 -------------------------------------------------------------------------
step(7, "并发回滚/热更新：只可能看到一个完整版本")
s, tpl_now = call("GET", f"/api/templates/{tid}")
print(f"     active before concurrent switch: {tpl_now['active']['semver']}")
# Pin a known baseline (v1); both ops believe v1 is live.
s, _ = call("POST", f"/api/templates/{tid}/activate",
            {"version_hash": v1h, "actor": "ops", "reason": "baseline to v1"})
# Two concurrent conflicting switches; exactly one may win. Whichever thread
# commits first, the other gets 409 (optimistic lock / no-op guard).
import concurrent.futures as cf
import threading

barrier = threading.Barrier(2)
def switch(target, label):
    barrier.wait()
    return label, call("POST", f"/api/templates/{tid}/activate",
                       {"version_hash": target, "actor": "ops", "reason": label,
                        "expected_active": v1h})
with cf.ThreadPoolExecutor(2) as ex:
    futs = [ex.submit(switch, v2h, "hot-update-to-v2"),
            ex.submit(switch, v1h, "rollback-stay-v1")]
    outcomes = [f.result() for f in futs]
for label, (st, payload) in outcomes:
    if st < 300:
        print(f"[{st}] {label}: WON  -> {payload['active']['semver']}")
    else:
        print(f"[{st}] {label}: LOST -> {payload['error']}: {payload['message'][:70]}")
s, tpl_final = call("GET", f"/api/templates/{tid}")
final_v = tpl_final["active"]["semver"]
print(f"     active after race: exactly one pointer -> {final_v}")
# Old request remains reviewable with its original metadata regardless.
s, old = call("GET", f"/api/requests/{rid}")
print(f"[{s}] old request metadata intact: served on {old['semver']} "
      f"fp={old['model_fingerprint']} input={old['input_digest'][:12]}…")
# Restore upstream model revision: with environment back to original state,
# the recorded request must reproduce byte-for-byte.
s, _ = call("POST", "/api/models/rotate", {"model_id": "llama-3.1-8b",
                                           "revision": "r2024-09-01"})
print(f"[{s}] upstream model revision restored for clean reproduction check")
s, final_rep = call("POST", f"/api/requests/{rid}/replay", {"mode": "recorded"})
print(f"[{s}] replay verdict of old request: {final_rep['verdict']} "
      f"(recorded version always re-runnable, output={final_rep['replay']['output']!r})")

print(f"\n{'='*72}\nDEMO COMPLETE — UI: {BASE}/  {BASE}/evaluate.html  {BASE}/replay.html\n{'='*72}")

"""End-to-end governance workflow tests (in-memory DB, deterministic provider)."""
import threading
import time
import unittest

from app.evaluation import EvaluationService, validate_inputs
from app.governance import (
    ApprovalRequiredError,
    ConflictError,
    Governance,
    PublishGateError,
    StateError,
    ValidationError,
)
from app.models import ModelProvider
from app.replay import ReplayService
from app.service import Service
from app.serving import Serving
from app.store import Database
from app import canonical


BASE_BODY = {
    "system_instructions": "You are a support agent for {{product}}. Q: {{question}}",
    "variables": {
        "product": {"type": "string", "required": True, "enum": ["billing", "cloud"]},
        "question": {"type": "string", "required": True},
    },
    "tools": ["kb.search"],
    "model_params": {"temperature": 0.2, "seed": 7, "max_tokens": 256},
    "model_id": "llama-3.1-8b",
}
CASES = [
    {"case_id": "c1", "inputs": {"product": "billing", "question": "refund?"},
     "expect": {"min_len": 3}},
    {"case_id": "c2", "inputs": {"product": "cloud", "question": "regions?"},
     "expect": {"min_len": 3}},
]
INPUTS = {"product": "billing", "question": "how do I get a refund?"}


def fresh_stack():
    db = Database(":memory:")
    provider = ModelProvider()
    gov = Governance(db, provider)
    ev = EvaluationService(db, provider, gov)
    srv = Serving(db, provider, gov)
    rep = ReplayService(db, provider, gov)
    return db, provider, gov, ev, srv, rep


def make_published(gov, ev, *, body=None, actor="alice"):
    """Create template -> v1 -> evaluate -> approve -> publish; returns ids."""
    body = body or BASE_BODY
    tpl = gov.create_template(name="support", actor=actor)
    v = gov.create_version(tpl["template_id"], body, actor)
    ts = ev.create_testset("golden", CASES, "qa")
    ev.freeze_testset(ts["testset_id"])
    e = ev.start_evaluation(v["version_hash"], ts["testset_id"],
                            background=False)
    assert e["status"] == "complete" and e["summary"]["pass_rate"] == 1.0
    apr = gov.request_approval(v["version_hash"], "publish", "qa-lead")
    gov.decide_approval(apr["approval_id"], True, "release-board")
    pub = gov.publish_version(v["version_hash"], "release-manager")
    gov.activate_version(tpl["template_id"], v["version_hash"], "ops")
    return {"tpl": tpl, "v": pub, "ts": ts, "eval": e}


class TestCanonical(unittest.TestCase):
    def test_hash_stable_regardless_of_order(self):
        a = canonical.normalize_body(BASE_BODY)
        b = canonical.normalize_body({**BASE_BODY,
                                      "variables": dict(reversed(list(BASE_BODY["variables"].items())))})
        self.assertEqual(canonical.content_hash(a), canonical.content_hash(b))

    def test_input_digest_distinguishes_content(self):
        d1 = canonical.input_digest({"a": 1, "b": 2})
        d2 = canonical.input_digest({"b": 2, "a": 1})
        d3 = canonical.input_digest({"a": 1, "b": 3})
        self.assertEqual(d1, d2)
        self.assertNotEqual(d1, d3)

    def test_contract_validation(self):
        contract = canonical.normalize_variables(BASE_BODY["variables"])
        clean = validate_inputs(contract, INPUTS)
        self.assertEqual(clean["product"], "billing")
        with self.assertRaises(ValidationError):
            validate_inputs(contract, {"product": "billing"})  # missing required
        with self.assertRaises(ValidationError):
            validate_inputs(contract, {**INPUTS, "product": "email"})  # enum
        with self.assertRaises(ValidationError):
            validate_inputs(contract, {**INPUTS, "extra": 1})  # undeclared


class TestVersioning(unittest.TestCase):
    def setUp(self):
        self.db, self.provider, self.gov, self.ev, self.srv, self.rep = fresh_stack()
        self.tpl = self.gov.create_template(name="t", actor="a")

    def test_initial_is_1_0_0_and_immutable_content(self):
        v = self.gov.create_version(self.tpl["template_id"], BASE_BODY, "a")
        self.assertEqual(v["semver"], "1.0.0")
        self.assertEqual(v["change_class"], "initial")
        self.assertTrue(v["version_hash"])
        # Duplicate content rejected.
        with self.assertRaises(ConflictError):
            self.gov.create_version(self.tpl["template_id"], BASE_BODY, "a")

    def test_patch_minor_major_classification(self):
        v1 = self.gov.create_version(self.tpl["template_id"], BASE_BODY, "a")["version_hash"]
        # patch: instructions only
        patch_body = {**BASE_BODY, "system_instructions": BASE_BODY["system_instructions"] + " Be polite."}
        self.assertEqual(self.gov.create_version(self.tpl["template_id"], patch_body, "a")["semver"], "1.0.1")
        # minor: add optional variable
        minor_body = {**patch_body, "variables": {
            **BASE_BODY["variables"], "locale": {"type": "string", "required": False, "default": "en"}}}
        self.assertEqual(self.gov.create_version(self.tpl["template_id"], minor_body, "a")["semver"], "1.1.0")
        # major: new required variable (contract break)
        major_body = {**minor_body, "variables": {
            **minor_body["variables"], "account_id": {"type": "string", "required": True}}}
        v = self.gov.create_version(self.tpl["template_id"], major_body, "a")
        self.assertEqual(v["semver"], "2.0.0")

    def test_tool_expansion_flagged(self):
        self.gov.create_version(self.tpl["template_id"], BASE_BODY, "a")
        body2 = {**BASE_BODY, "tools": ["kb.search", "billing.refund"],
                 "system_instructions": BASE_BODY["system_instructions"] + " x"}
        v2 = self.gov.create_version(self.tpl["template_id"], body2, "a")
        self.assertEqual(v2["change_class"], "major")
        self.assertIsNotNone(v2["expansion_of_hash"])

        # A pure tool *reduction* needs no expansion approval basis.
        body3 = {**BASE_BODY, "tools": [],
                 "system_instructions": BASE_BODY["system_instructions"] + " y"}
        v3 = self.gov.create_version(self.tpl["template_id"], body3, "a")
        self.assertIsNone(v3["expansion_of_hash"])
        with self.assertRaises(StateError):
            self.gov.request_approval(v3["version_hash"], "permission_expansion", "a")

    def test_tool_expansion_request_succeeds(self):
        self.gov.create_version(self.tpl["template_id"], BASE_BODY, "a")
        body2 = {**BASE_BODY, "tools": ["kb.search", "billing.refund"],
                 "system_instructions": BASE_BODY["system_instructions"] + " x"}
        v2 = self.gov.create_version(self.tpl["template_id"], body2, "a")
        apr = self.gov.request_approval(v2["version_hash"], "permission_expansion", "a")
        self.assertEqual(apr["grants_expansion"]["added"], ["billing.refund"])
        self.assertEqual(apr["grants_snapshot_hash"], v2["grants_snapshot_hash"])


class TestPublishGate(unittest.TestCase):
    def setUp(self):
        self.db, self.provider, self.gov, self.ev, self.srv, self.rep = fresh_stack()
        self.stack = make_published(self.gov, self.ev)
        self.tid = self.stack["tpl"]["template_id"]

    def test_publish_requires_evidence_and_approval(self):
        tpl = self.gov.create_template(name="t2", actor="a")
        v = self.gov.create_version(tpl["template_id"], BASE_BODY, "a")
        # no evidence -> gate
        with self.assertRaises(PublishGateError):
            self.gov.publish_version(v["version_hash"], "rm")
        ts = self.ev.create_testset("g2", CASES, "qa")
        self.ev.freeze_testset(ts["testset_id"])
        self.ev.start_evaluation(v["version_hash"], ts["testset_id"], background=False)
        # evidence but no approval -> still gate
        with self.assertRaises(PublishGateError):
            self.gov.publish_version(v["version_hash"], "rm")

    def test_published_version_is_immutable(self):
        v = self.gov.get_version(self.stack["v"]["version_hash"])
        self.assertEqual(v["status"], "published")
        # Pinned evidence must not change even if a (late) evaluation arrives.
        pinned = v["evidence_hash"]
        late = self.ev.start_evaluation(v["version_hash"], self.stack["ts"]["testset_id"],
                                        delay=0.0, background=True)
        self.ev.finish_latest_evaluations()
        late2 = self.ev.get_evaluation(late["eval_id"])
        self.assertTrue(late2["late"])
        self.assertEqual(late2["publish_ref"], pinned)
        v2 = self.gov.get_version(v["version_hash"])
        self.assertEqual(v2["evidence_hash"], pinned)

    def test_late_evaluation_before_publish_marked(self):
        tpl = self.gov.create_template(name="late", actor="a")
        v = self.gov.create_version(tpl["template_id"], BASE_BODY, "a")
        ts = self.ev.create_testset("g3", CASES, "qa")
        self.ev.freeze_testset(ts["testset_id"])
        # Start a *quick* evaluation synchronously, publish, then start a late one.
        self.ev.start_evaluation(v["version_hash"], ts["testset_id"], background=False)
        apr = self.gov.request_approval(v["version_hash"], "publish", "x")
        self.gov.decide_approval(apr["approval_id"], True, "y")
        self.gov.publish_version(v["version_hash"], "rm")
        late = self.ev.start_evaluation(v["version_hash"], ts["testset_id"], background=False)
        self.assertTrue(late["late"])
        self.assertIsNotNone(late["publish_ref"])


class TestExpansionApprovalGate(unittest.TestCase):
    def setUp(self):
        self.db, self.provider, self.gov, self.ev, self.srv, self.rep = fresh_stack()
        self.stack = make_published(self.gov, self.ev)
        self.tid = self.stack["tpl"]["template_id"]

    def _v2_expanded(self):
        body2 = {**BASE_BODY, "tools": ["kb.search", "billing.refund"],
                 "system_instructions": BASE_BODY["system_instructions"] + " Be careful."}
        return self.gov.create_version(self.tid, body2, "a")

    def _prepare_v2(self, v2):
        ts = self.ev.create_testset("g-exp", CASES, "qa")
        self.ev.freeze_testset(ts["testset_id"])
        self.ev.start_evaluation(v2["version_hash"], ts["testset_id"], background=False)
        apr = self.gov.request_approval(v2["version_hash"], "publish", "x")
        self.gov.decide_approval(apr["approval_id"], True, "y")

    def test_expansion_requires_specific_approval(self):
        v2 = self._v2_expanded()
        self._prepare_v2(v2)
        with self.assertRaises(ApprovalRequiredError):
            self.gov.publish_version(v2["version_hash"], "rm")
        exp = self.gov.request_approval(v2["version_hash"], "permission_expansion", "eng")
        # security board rejects at first
        self.gov.decide_approval(exp["approval_id"], False, "security")
        with self.assertRaises(ApprovalRequiredError):
            self.gov.publish_version(v2["version_hash"], "rm")
        # New approval after rejection.
        exp2 = self.gov.request_approval(v2["version_hash"], "permission_expansion", "eng")
        self.gov.decide_approval(exp2["approval_id"], True, "security")
        pub = self.gov.publish_version(v2["version_hash"], "rm")
        self.assertEqual(pub["status"], "published")

    def test_expansion_approval_bound_to_exact_snapshot(self):
        v2 = self._v2_expanded()
        exp = self.gov.request_approval(v2["version_hash"], "permission_expansion", "eng")
        # The approval row must pin the grants snapshot hash of THIS version.
        got = self.gov.list_approvals(version_hash=v2["version_hash"], status="requested")[0]
        self.assertEqual(got["grants_snapshot_hash"], v2["grants_snapshot_hash"])
        self.assertEqual(got["grants_expansion"]["added"], ["billing.refund"])


class TestServingAndReplay(unittest.TestCase):
    def setUp(self):
        self.db, self.provider, self.gov, self.ev, self.srv, self.rep = fresh_stack()
        self.stack = make_published(self.gov, self.ev)
        self.tid = self.stack["tpl"]["template_id"]
        self.req = self.srv.infer(self.tid, INPUTS, actor="c")
        self.rid = self.req["request_id"]

    def test_request_binds_exact_metadata(self):
        b = self.req["binding"]
        self.assertEqual(b["version_hash"], self.stack["v"]["version_hash"])
        self.assertEqual(b["model_id"], "llama-3.1-8b")
        self.assertEqual(b["input_digest"], canonical.input_digest(INPUTS))

    def test_recorded_replay_is_exact_reproduction(self):
        r = self.rep.replay(self.rid, mode="recorded")
        self.assertEqual(r["verdict"], "exact_reproduction")
        self.assertTrue(r["reproduced"])
        self.assertEqual(r["axes_changed"], [])
        self.assertEqual(r["original"]["output"], r["replay"]["output"])

    def test_input_change_attributed(self):
        r = self.rep.replay(self.rid, mode="recorded",
                            inputs_override={**INPUTS, "question": "different question entirely"})
        self.assertIn("INPUT", r["axes_changed"])
        self.assertTrue(r["axes"]["INPUT"]["changed"])

    def test_model_change_attributed_after_silent_rotation(self):
        self.provider.rotate("llama-3.1-8b", "r2025-01-01")
        r = self.rep.replay(self.rid, mode="recorded")
        self.assertIn("MODEL", r["axes_changed"])
        self.assertTrue(r["axes"]["MODEL"]["changed"])
        self.assertEqual(r["axes"]["MODEL"]["detail"]["current_revision"], "r2025-01-01")
        # output may or may not differ, but model axis moved; no unexpected drift
        self.assertNotEqual(r["verdict"], "unexpected_drift")

    def test_template_change_attributed_after_hot_update(self):
        body2 = {**BASE_BODY, "system_instructions": BASE_BODY["system_instructions"] + " Say hi."}
        v2 = self.gov.create_version(self.tid, body2, "a")
        ts = self.ev.create_testset("g-hot", CASES, "qa")
        self.ev.freeze_testset(ts["testset_id"])
        self.ev.start_evaluation(v2["version_hash"], ts["testset_id"], background=False)
        apr = self.gov.request_approval(v2["version_hash"], "publish", "x")
        self.gov.decide_approval(apr["approval_id"], True, "y")
        self.gov.publish_version(v2["version_hash"], "rm")
        self.gov.activate_version(self.tid, v2["version_hash"], "ops")

        # recorded mode: still reproduces the OLD request from old metadata
        r_old = self.rep.replay(self.rid, mode="recorded")
        self.assertEqual(r_old["verdict"], "exact_reproduction")
        self.assertTrue(r_old["axes"]["TEMPLATE"]["changed"])  # active moved...
        # ...but recorded execution pins the original version hash
        self.assertEqual(r_old["replay"]["fingerprints"]["version_hash"],
                         self.stack["v"]["version_hash"])

        # active mode: executes the new template with the old inputs
        r_act = self.rep.replay(self.rid, mode="active")
        self.assertIn("TEMPLATE", r_act["axes_changed"])
        self.assertEqual(r_act["replay"]["fingerprints"]["version_hash"], v2["version_hash"])

        # new traffic binds to new version
        new_req = self.srv.infer(self.tid, INPUTS)
        self.assertEqual(new_req["binding"]["version_hash"], v2["version_hash"])

    def test_old_request_reviewable_after_rollback(self):
        # Publish v2 (instructions only -> patch) and hot update.
        body2 = {**BASE_BODY, "system_instructions": "Revised instructions. {{product}} {{question}}"}
        v2 = self.gov.create_version(self.tid, body2, "a")
        ts = self.ev.create_testset("g-rb", CASES, "qa")
        self.ev.freeze_testset(ts["testset_id"])
        self.ev.start_evaluation(v2["version_hash"], ts["testset_id"], background=False)
        apr = self.gov.request_approval(v2["version_hash"], "publish", "x")
        self.gov.decide_approval(apr["approval_id"], True, "y")
        self.gov.publish_version(v2["version_hash"], "rm")
        self.gov.activate_version(self.tid, v2["version_hash"], "ops")
        # rollback to v1
        self.gov.rollback(self.tid, self.stack["v"]["version_hash"], "ops")
        active = self.gov.get_active_version(self.tid)
        self.assertEqual(active["version_hash"], self.stack["v"]["version_hash"])
        # Old request still reviews against original metadata; TEMPLATE axis
        # reports same content/version now (rolled back to what served it).
        r = self.rep.replay(self.rid, mode="recorded")
        self.assertEqual(r["verdict"], "exact_reproduction")
        self.assertFalse(r["axes"]["TEMPLATE"]["changed"])
        deploys = self.gov.list_deployments(self.tid)
        self.assertEqual([d["action"] for d in deploys], ["activate", "activate", "rollback"])


class TestAtomicSwitch(unittest.TestCase):
    def test_concurrent_activations_one_winner(self):
        db, provider, gov, ev, srv, rep = fresh_stack()
        # publish v1 and v2
        s1 = make_published(gov, ev)
        tid = s1["tpl"]["template_id"]
        body2 = {**BASE_BODY, "system_instructions": BASE_BODY["system_instructions"] + " V2."}
        v2 = gov.create_version(tid, body2, "a")
        ts = ev.create_testset("g-c", CASES, "qa"); ev.freeze_testset(ts["testset_id"])
        ev.start_evaluation(v2["version_hash"], ts["testset_id"], background=False)
        apr = gov.request_approval(v2["version_hash"], "publish", "x")
        gov.decide_approval(apr["approval_id"], True, "y")
        gov.publish_version(v2["version_hash"], "rm")

        v1h, v2h = s1["v"]["version_hash"], v2["version_hash"]
        barrier = threading.Barrier(2)
        results = []

        def switch(target):
            barrier.wait()
            try:
                gov.activate_version(tid, target, "ops", reason="concurrent")
                results.append(("ok", target))
            except ConflictError:
                results.append(("conflict", target))
            except Exception as e:  # noqa
                results.append(("other", repr(e)))

        t1 = threading.Thread(target=switch, args=(v2h,))
        t2 = threading.Thread(target=switch, args=(v1h,))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = sorted(s for s, _ in results)
        self.assertIn("conflict", statuses)
        self.assertIn("ok", statuses)
        # Exactly one active pointer, pointing at one complete published version.
        active = gov.get_active_version(tid)
        self.assertIn(active["version_hash"], (v1h, v2h))
        self.assertEqual(active["status"], "published")

    def test_expected_active_optimistic_lock(self):
        db, provider, gov, ev, srv, rep = fresh_stack()
        s1 = make_published(gov, ev)
        tid = s1["tpl"]["template_id"]
        body2 = {**BASE_BODY, "system_instructions": BASE_BODY["system_instructions"] + " Y."}
        v2 = gov.create_version(tid, body2, "a")
        ts = ev.create_testset("g-ol", CASES, "qa"); ev.freeze_testset(ts["testset_id"])
        ev.start_evaluation(v2["version_hash"], ts["testset_id"], background=False)
        apr = gov.request_approval(v2["version_hash"], "publish", "x")
        gov.decide_approval(apr["approval_id"], True, "y")
        gov.publish_version(v2["version_hash"], "rm")
        v1h = s1["v"]["version_hash"]
        # Stale expected pointer -> 409
        with self.assertRaises(ConflictError):
            gov.activate_version(tid, v2["version_hash"], "ops",
                                 expected_active="deadbeef" * 8)
        # Correct expectation succeeds
        gov.activate_version(tid, v2["version_hash"], "ops", expected_active=v1h)


class TestServiceWiring(unittest.TestCase):
    def test_full_flow_via_service(self):
        svc = Service(":memory:")
        tpl = svc.gov.create_template(name="x", actor="a")
        v = svc.gov.create_version(tpl["template_id"], BASE_BODY, "a")
        ts = svc.evals.create_testset("g", CASES, "qa")
        svc.evals.freeze_testset(ts["testset_id"])
        e = svc.evals.start_evaluation(v["version_hash"], ts["testset_id"], background=False)
        self.assertEqual(e["summary"]["pass_rate"], 1.0)
        svc.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

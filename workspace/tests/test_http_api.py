"""HTTP API smoke/integration tests (real server on an ephemeral port)."""
import json
import threading
import unittest
import urllib.error
import urllib.request

from app.http_api import ApiServer
from app.service import Service


BODY = {
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


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = Service(":memory:")
        cls.server = ApiServer(("127.0.0.1", 0), cls.svc, quiet=True)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.svc.close()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def publish_one(self):
        _, tpl = self.call("POST", "/api/templates", {"name": "t", "actor": "a"})
        _, v = self.call("POST", f"/api/templates/{tpl['template_id']}/versions", {**BODY, "actor": "a"})
        _, ts = self.call("POST", "/api/testsets", {"name": "g", "cases": CASES, "actor": "q"})
        self.call("POST", f"/api/testsets/{ts['testset_id']}/freeze", {})
        _, ev = self.call("POST", f"/api/versions/{v['version_hash']}/evaluate",
                          {"testset_id": ts["testset_id"], "background": False})
        _, apr = self.call("POST", f"/api/versions/{v['version_hash']}/approvals",
                           {"kind": "publish", "actor": "x"})
        self.call("POST", f"/api/approvals/{apr['approval_id']}/decision",
                  {"approve": True, "actor": "y"})
        self.call("POST", f"/api/versions/{v['version_hash']}/publish", {"actor": "rm"})
        self.call("POST", f"/api/templates/{tpl['template_id']}/activate",
                  {"version_hash": v["version_hash"], "actor": "ops"})
        return tpl, v

    def test_health_and_pages(self):
        for p in ("/health", "/", "/evaluate.html", "/replay.html", "/app.css", "/app.js"):
            with urllib.request.urlopen(self.base + p) as r:
                self.assertEqual(r.status, 200, p)

    def test_404_and_bad_json(self):
        s, _ = self.call("GET", "/api/nope")
        self.assertEqual(s, 404)
        req = urllib.request.Request(
            self.base + "/api/templates", data=b"not json", method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            self.fail("expected error")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_full_publish_infer_replay(self):
        tpl, v = self.publish_one()
        s, req = self.call("POST", f"/api/templates/{tpl['template_id']}/infer",
                           {"inputs": {"product": "billing", "question": "hi?"}})
        self.assertEqual(s, 200)
        self.assertEqual(req["binding"]["version_hash"], v["version_hash"])
        s, rep = self.call("POST", f"/api/requests/{req['request_id']}/replay",
                           {"mode": "recorded"})
        self.assertEqual(s, 200)
        self.assertEqual(rep["verdict"], "exact_reproduction")

    def test_expansion_blocked_over_http(self):
        tpl, v1 = self.publish_one()
        body2 = {**BODY,
                 "system_instructions": BODY["system_instructions"] + " Refunds allowed.",
                 "tools": ["kb.search", "billing.refund"]}
        _, v2 = self.call("POST", f"/api/templates/{tpl['template_id']}/versions",
                          {**body2, "actor": "b"})
        _, ts = self.call("GET", "/api/testsets")
        tsid = ts[0]["testset_id"]
        self.call("POST", f"/api/versions/{v2['version_hash']}/evaluate",
                  {"testset_id": tsid, "background": False})
        _, apr = self.call("POST", f"/api/versions/{v2['version_hash']}/approvals",
                           {"kind": "publish", "actor": "x"})
        self.call("POST", f"/api/approvals/{apr['approval_id']}/decision",
                  {"approve": True, "actor": "y"})
        s, err = self.call("POST", f"/api/versions/{v2['version_hash']}/publish", {"actor": "rm"})
        self.assertEqual(s, 403)
        self.assertIn("expansion", err["message"])

    def test_contract_violation_rejected(self):
        tpl, v = self.publish_one()
        s, err = self.call("POST", f"/api/templates/{tpl['template_id']}/infer",
                           {"inputs": {"product": "email", "question": "x"}})
        self.assertEqual(s, 400)
        self.assertEqual(err["error"], "ValidationError")

    def test_activate_unpublished_rejected(self):
        _, tpl = self.call("POST", "/api/templates", {"name": "t2", "actor": "a"})
        _, v = self.call("POST", f"/api/templates/{tpl['template_id']}/versions",
                         {**BODY, "actor": "a"})
        s, err = self.call("POST", f"/api/templates/{tpl['template_id']}/activate",
                           {"version_hash": v["version_hash"]})
        self.assertEqual(s, 409)


if __name__ == "__main__":
    unittest.main(verbosity=2)

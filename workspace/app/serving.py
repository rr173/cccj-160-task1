"""Online serving: every inference is bound to exact version/model/input.

A request row pins:
  version_hash, semver, model_id, model_fingerprint, model_revision,
  input_digest (canonical hash of contract-validated inputs), params,
  prompt_sha and output text.

Even after hot update / rollback, an old request keeps its original metadata,
so replays can compare *what actually served it* against *what would serve it
now* along three independent axes.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import canonical
from .evaluation import validate_inputs
from .governance import Governance, new_id, now
from .models import ModelProvider
from .store import Database, jloads


class Serving:
    def __init__(self, db: Database, provider: ModelProvider, gov: Governance):
        self.db = db
        self.provider = provider
        self.gov = gov

    def infer(self, template_id: str, inputs: dict, *, actor: str = "client") -> dict:
        # Read the single atomic live pointer. Because the pointer switches in
        # one committed transaction, this is always one *complete* version.
        active = self.gov.get_active_version(template_id)
        body = active["body"]
        contract = body["variables"]
        clean = validate_inputs(contract, inputs)

        resp = self.provider.complete(
            model_id=body["model_id"],
            system_instructions=body["system_instructions"],
            variables=clean,
            model_params=body["model_params"],
        )
        digest = canonical.input_digest(clean)
        rid = new_id("req")
        ts = now()
        conn = self.db.conn()
        conn.execute(
            """INSERT INTO requests(
                request_id,template_id,version_hash,semver,model_id,
                model_fingerprint,model_revision,inputs_json,input_digest,
                output_text,params_json,prompt_sha,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, template_id, active["version_hash"], active["semver"],
             resp.model_id, resp.fingerprint, resp.revision,
             json.dumps(clean, ensure_ascii=False), digest,
             resp.text,
             json.dumps(body["model_params"], ensure_ascii=False),
             resp.prompt_sha, ts),
        )
        self.gov._audit(conn, "request", rid, "infer", actor,
                        {"version": active["version_hash"], "input_digest": digest})
        return {
            "request_id": rid,
            "output": resp.text,
            "binding": {
                "template_id": template_id,
                "version_hash": active["version_hash"],
                "semver": active["semver"],
                "content_hash": active["content_hash"],
                "model_id": resp.model_id,
                "model_revision": resp.revision,
                "model_fingerprint": resp.fingerprint,
                "input_digest": digest,
                "prompt_sha": resp.prompt_sha,
                "params": body["model_params"],
                "served_at": ts,
            },
        }

    def get_request(self, request_id: str) -> dict:
        conn = self.db.conn()
        row = conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            from .governance import NotFoundError
            raise NotFoundError(f"request {request_id} not found")
        d = dict(row)
        d["inputs"] = jloads(d.pop("inputs_json"))
        d["params"] = jloads(d.pop("params_json"))
        return d

    def list_requests(self, template_id: str | None = None, limit: int = 100) -> list[dict]:
        conn = self.db.conn()
        if template_id:
            rows = conn.execute(
                "SELECT * FROM requests WHERE template_id=? ORDER BY rowid DESC LIMIT ?",
                (template_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM requests ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["inputs"] = jloads(d.pop("inputs_json"))
            d["params"] = jloads(d.pop("params_json"))
            out.append(d)
        return out

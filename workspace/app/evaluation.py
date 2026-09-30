"""Fixed test sets, evaluation evidence, and variable-contract validation.

Evidence properties:
* the test set is *frozen* (snapshot hash captured) before any run against a
  version; subsequent edits to the set are impossible
* every per-case record stores the exact model_id / active revision /
  fingerprint and prompt hash, so evidence pins model provenance too
* evidence_hash covers rows + summary + frozen testset hash -> tamper evident
* evaluations finishing after the version was published are flagged late=1 and
  linked to the publish evidence; they never overwrite the pinned snapshot
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from . import canonical
from .governance import (
    ConflictError,
    Governance,
    NotFoundError,
    ValidationError,
    new_id,
    now,
)
from .models import ModelProvider
from .store import Database, jloads

_PY_TYPES = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
}


def validate_inputs(contract: dict, inputs: Any) -> dict:
    """Validate raw request/eval inputs against the variable contract.

    Returns a coerced/normalized mapping (only declared variables, canonical
    ordering handled by canonical.input_digest). Undeclared variables are
    rejected so the input digest has unambiguous meaning.
    """
    if not isinstance(inputs, dict):
        raise ValidationError("inputs must be an object")
    unknown = sorted(set(inputs) - set(contract))
    if unknown:
        raise ValidationError(f"undeclared variables: {unknown}")

    clean: dict[str, Any] = {}
    for name, spec in contract.items():
        if name in inputs:
            value = inputs[name]
            vtype = spec["type"]
            if vtype == "integer" and isinstance(value, bool):
                raise ValidationError(f"variable {name!r}: expected integer, got boolean")
            if vtype == "number" and isinstance(value, bool):
                raise ValidationError(f"variable {name!r}: expected number, got boolean")
            if not isinstance(value, _PY_TYPES[vtype]):
                raise ValidationError(f"variable {name!r}: expected {vtype}")
            if "enum" in spec and value not in spec["enum"]:
                raise ValidationError(f"variable {name!r}: {value!r} not in enum {spec['enum']}")
            clean[name] = value
        elif "default" in spec:
            clean[name] = spec["default"]
        elif spec.get("required"):
            raise ValidationError(f"missing required variable {name!r}")
    return clean


def _check_expectation(output: str, expect: dict) -> tuple[bool, float, dict | None]:
    """Evaluate a case expectation.

    expect forms:
      {"contains": "text"}
      {"equals": "text"}
      {"regex": "pattern"}
      {"min_len": n}
    Multiple keys are AND-combined. score = 1.0 pass / 0.0 fail.
    """
    import re
    failures = []
    if not isinstance(expect, dict) or not expect:
        return True, 1.0, None
    if "contains" in expect and expect["contains"] not in output:
        failures.append({"check": "contains", "expected": expect["contains"]})
    if "equals" in expect and output != expect["equals"]:
        failures.append({"check": "equals", "expected": expect["equals"]})
    if "regex" in expect and not re.search(expect["regex"], output):
        failures.append({"check": "regex", "expected": expect["regex"]})
    if "min_len" in expect and len(output) < int(expect["min_len"]):
        failures.append({"check": "min_len", "expected": expect["min_len"],
                         "actual": len(output)})
    if failures:
        return False, 0.0, {"failures": failures}
    return True, 1.0, None


class EvaluationService:
    def __init__(self, db: Database, provider: ModelProvider, gov: Governance):
        self.db = db
        self.provider = provider
        self.gov = gov
        self._eval_lock = threading.RLock()

    # -- test sets ---------------------------------------------------------

    def create_testset(self, name: str, cases: list[dict], actor: str,
                       description: str = "") -> dict:
        if not name or not name.strip():
            raise ValidationError("testset name required")
        norm_cases = self._normalize_cases(cases)
        chash = canonical.sha256_hex(
            {"spec": canonical.SPEC_VERSION, "kind": "testset",
             "name": name.strip(), "cases": norm_cases}
        )
        tsid = new_id("set")
        conn = self.db.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO testsets(testset_id,name,description,content_hash,frozen,"
                "created_by,created_at) VALUES(?,?,?,?,0,?,?)",
                (tsid, name.strip(), description, chash, actor, now()),
            )
            for c in norm_cases:
                conn.execute(
                    "INSERT INTO test_cases(testset_id,case_id,inputs_json,expect_json)"
                    " VALUES(?,?,?,?)",
                    (tsid, c["case_id"],
                     json.dumps(c["inputs"], ensure_ascii=False),
                     json.dumps(c["expect"], ensure_ascii=False)),
                )
            self.gov._audit(conn, "testset", tsid, "create", actor,
                            {"cases": len(norm_cases)})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return self.get_testset(tsid)

    @staticmethod
    def _normalize_cases(cases: Any) -> list[dict]:
        if not isinstance(cases, list) or not cases:
            raise ValidationError("cases must be a non-empty list")
        out = []
        seen = set()
        for i, c in enumerate(cases):
            if not isinstance(c, dict) or "inputs" not in c:
                raise ValidationError(f"case #{i}: object with 'inputs' required")
            cid = str(c.get("case_id") or f"case-{i+1:03d}")
            if cid in seen:
                raise ValidationError(f"duplicate case_id {cid}")
            seen.add(cid)
            if not isinstance(c["inputs"], dict):
                raise ValidationError(f"case {cid}: inputs must be an object")
            expect = c.get("expect", {})
            out.append({"case_id": cid, "inputs": c["inputs"], "expect": expect})
        return out

    def freeze_testset(self, testset_id: str) -> dict:
        with self._eval_lock:
            conn = self.db.conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_set_row(conn, testset_id)
                if row["frozen"]:
                    return self.get_testset(testset_id)
                conn.execute(
                    "UPDATE testsets SET frozen=1, frozen_at=? WHERE testset_id=?",
                    (now(), testset_id),
                )
                self.gov._audit(conn, "testset", testset_id, "freeze", "system", {})
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get_testset(testset_id)

    @staticmethod
    def _get_set_row(conn, testset_id: str):
        row = conn.execute("SELECT * FROM testsets WHERE testset_id=?", (testset_id,)).fetchone()
        if not row:
            raise NotFoundError(f"testset {testset_id} not found")
        return row

    def get_testset(self, testset_id: str) -> dict:
        conn = self.db.conn()
        row = self._get_set_row(conn, testset_id)
        out = dict(row)
        out["frozen"] = bool(row["frozen"])
        cases = conn.execute(
            "SELECT case_id, inputs_json, expect_json FROM test_cases WHERE testset_id=?",
            (testset_id,),
        ).fetchall()
        out["cases"] = [
            {"case_id": c["case_id"], "inputs": jloads(c["inputs_json"]),
             "expect": jloads(c["expect_json"])}
            for c in cases
        ]
        return out

    def list_testsets(self) -> list[dict]:
        conn = self.db.conn()
        out = []
        for r in conn.execute("SELECT testset_id,name,frozen,content_hash,created_at"
                              " FROM testsets ORDER BY created_at"):
            d = dict(r); d["frozen"] = bool(r["frozen"])
            out.append(d)
        return out

    # -- evaluation runs ---------------------------------------------------

    def start_evaluation(self, version_hash: str, testset_id: str,
                         *, actor: str = "ci", delay: float = 0.0,
                         background: bool = True) -> dict:
        """Run the fixed test set against a version.

        delay>0 simulates *late* evidence (the evaluation is stored in running
        state and completed by finish_latest_evaluations), exercising the path
        where publish happens before results land.
        """
        conn = self.db.conn()
        v = self.gov._get_version_row(conn, version_hash)
        ts = self._get_set_row(conn, testset_id)
        if not ts["frozen"]:
            # Freezing on first use pins the fixed set for this version line.
            self.freeze_testset(testset_id)
            ts = self._get_set_row(conn, testset_id)

        cases = conn.execute(
            "SELECT case_id, inputs_json, expect_json FROM test_cases WHERE testset_id=?",
            (testset_id,),
        ).fetchall()

        eval_id = new_id("eval")
        conn.execute("BEGIN IMMEDIATE")
        try:
            # If the version is already published, this run is late by nature.
            published = v["status"] == "published"
            conn.execute(
                """INSERT INTO evaluations(
                    eval_id,template_id,version_hash,testset_id,testset_hash,
                    status,late,publish_ref,started_at)
                   VALUES(?,?,?,?,?,'running',?,?,?)""",
                (eval_id, v["template_id"], version_hash, testset_id, ts["content_hash"],
                 1 if published else 0,
                 v["evidence_hash"] if published else None, now()),
            )
            self.gov._audit(conn, "evaluation", eval_id, "start", actor,
                            {"version": version_hash, "testset": testset_id})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        payload = {
            "eval_id": eval_id,
            "version_hash": version_hash,
            "testset_id": testset_id,
            "cases": [
                {"case_id": c["case_id"], "inputs": jloads(c["inputs_json"]),
                 "expect": jloads(c["expect_json"])}
                for c in cases
            ],
            "actor": actor,
        }
        if background:
            t = threading.Thread(
                target=self._run_with_delay, args=(payload, delay), daemon=True
            )
            t.start()
        else:
            if delay:
                raise ValidationError("delay requires background=True")
            self._run_evaluation(payload)
        return self.get_evaluation(eval_id)

    def _run_with_delay(self, payload: dict, delay: float) -> None:
        if delay:
            time.sleep(delay)
        try:
            self._run_evaluation(payload)
        except Exception as exc:  # never let the thread die silently
            conn = self.db.conn()
            conn.execute(
                "UPDATE evaluations SET status='failed', error=?, finished_at=? WHERE eval_id=?",
                (repr(exc), now(), payload["eval_id"]),
            )

    def _run_evaluation(self, payload: dict) -> None:
        eval_id = payload["eval_id"]
        with self._eval_lock:
            conn = self.db.conn()
            v = self.gov._get_version_row(conn, payload["version_hash"])
            body = self.gov.version_body(v)
            rows = []
            stored = 0
            passed = 0
            for case in payload["cases"]:
                inputs = validate_inputs(body["variables"], case["inputs"])
                resp = self.provider.complete(
                    model_id=body["model_id"],
                    system_instructions=body["system_instructions"],
                    variables=inputs,
                    model_params=body["model_params"],
                )
                ok, score, failure = _check_expectation(resp.text, case["expect"])
                passed += ok
                rows.append({
                    "case_id": case["case_id"],
                    "output": resp.text,
                    "passed": ok,
                    "score": score,
                    "failure": failure,
                    "model_fingerprint": resp.fingerprint,
                    "model_revision": resp.revision,
                    "prompt_sha": resp.prompt_sha,
                })
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        """INSERT INTO eval_results(
                            eval_id,case_id,inputs_json,expected_json,output_text,
                            passed,score,failure_json,model_id,model_revision,
                            model_fingerprint,prompt_sha)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (eval_id, case["case_id"],
                         json.dumps(inputs, ensure_ascii=False),
                         json.dumps(case["expect"], ensure_ascii=False),
                         resp.text, 1 if ok else 0, score,
                         json.dumps(failure, ensure_ascii=False) if failure else None,
                         resp.model_id, resp.revision, resp.fingerprint, resp.prompt_sha),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                stored += 1

            total = len(rows)
            summary = {
                "total": total,
                "passed": passed,
                "failed": total - passed,
                "pass_rate": passed / total,
                "evaluated_at": now(),
                "model_id": body["model_id"],
                "model_fingerprint": self.provider.fingerprint(body["model_id"]),
            }
            # Evidence rows are the minimal signed payload per case.
            evidence_rows = [
                {k: r[k] for k in ("case_id", "output", "passed", "score",
                                   "failure", "model_fingerprint", "model_revision",
                                   "prompt_sha")}
                for r in rows
            ]
            conn.execute("BEGIN IMMEDIATE")
            try:
                # testset snapshot hash for evidence was captured at start.
                tshash = conn.execute(
                    "SELECT testset_hash FROM evaluations WHERE eval_id=?", (eval_id,)
                ).fetchone()["testset_hash"]
                eh = canonical.evidence_hash(evidence_rows, summary, tshash)
                cur = conn.execute(
                    "SELECT late,publish_ref,status FROM evaluations WHERE eval_id=?",
                    (eval_id,),
                ).fetchone()
                late = cur["late"]
                publish_ref = cur["publish_ref"]
                if cur["status"] != "running":
                    raise ConflictError(f"evaluation {eval_id} no longer running")
                # If publish raced ahead while the run executed, it's late.
                v_now = self.gov._get_version_row(conn, payload["version_hash"])
                if v_now["status"] == "published" and not late:
                    late = 1
                    publish_ref = v_now["evidence_hash"]
                conn.execute(
                    "UPDATE evaluations SET status='complete', late=?, publish_ref=?,"
                    " summary_json=?, evidence_hash=?, finished_at=? WHERE eval_id=?",
                    (1 if late else 0, publish_ref,
                     json.dumps(summary, ensure_ascii=False), eh, now(), eval_id),
                )
                self.gov._audit(conn, "evaluation", eval_id, "complete",
                                payload.get("actor", "ci"),
                                {"late": bool(late), "pass_rate": summary["pass_rate"],
                                 "evidence_hash": eh})
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def finish_latest_evaluations(self, timeout: float = 5.0) -> int:
        """Wait until no evaluations are running (used after delay-based late runs)."""
        deadline = time.time() + timeout
        conn = self.db.conn()
        while time.time() < deadline:
            n = conn.execute(
                "SELECT COUNT(*) c FROM evaluations WHERE status='running'"
            ).fetchone()["c"]
            if n == 0:
                return 0
            time.sleep(0.05)
        return conn.execute(
            "SELECT COUNT(*) c FROM evaluations WHERE status='running'"
        ).fetchone()["c"]

    def get_evaluation(self, eval_id: str, *, with_results: bool = True) -> dict:
        conn = self.db.conn()
        row = conn.execute("SELECT * FROM evaluations WHERE eval_id=?", (eval_id,)).fetchone()
        if not row:
            raise NotFoundError(f"evaluation {eval_id} not found")
        out = dict(row)
        out["late"] = bool(row["late"])
        out["summary"] = jloads(row["summary_json"])
        if with_results:
            res = conn.execute(
                "SELECT * FROM eval_results WHERE eval_id=? ORDER BY case_id", (eval_id,)
            ).fetchall()
            out["results"] = []
            for r in res:
                d = dict(r)
                d["inputs"] = jloads(d.pop("inputs_json"))
                d["expected"] = jloads(d.pop("expected_json"))
                d["failure"] = jloads(d.pop("failure_json"))
                d["passed"] = bool(r["passed"])
                out["results"].append(d)
        return out

    def list_evaluations(self, version_hash: str | None = None) -> list[dict]:
        conn = self.db.conn()
        q = "SELECT eval_id,version_hash,testset_id,testset_hash,status,late,publish_ref,"
        q += "evidence_hash,summary_json,started_at,finished_at FROM evaluations"
        args: list[Any] = []
        if version_hash:
            q += " WHERE version_hash=?"; args.append(version_hash)
        q += " ORDER BY started_at"
        out = []
        for r in conn.execute(q, args):
            d = dict(r); d["late"] = bool(r["late"])
            d["summary"] = jloads(d.pop("summary_json"))
            out.append(d)
        return out

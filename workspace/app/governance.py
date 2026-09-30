"""Governance service: templates, versions, approvals, publish gate, deploys.

All multi-statement state changes run inside one SQLite transaction guarded by
a process-wide lock, so hot updates / concurrent rollbacks are serialized:
readers either observe the old complete version or the new complete version,
never a half-switched pointer. The losing concurrent switch is rejected 409.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from . import canonical
from .diffing import body_diff, classify_change, next_semver
from .models import ModelProvider
from .store import Database, jloads, row_to_dict

PASS_RATE_GATE = 1.0  # fixed test set must fully pass before publish


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class GovernanceError(Exception):
    status = 400


class ValidationError(GovernanceError):
    status = 400


class NotFoundError(GovernanceError):
    status = 404


class ConflictError(GovernanceError):
    status = 409


class StateError(GovernanceError):
    status = 409


class ApprovalRequiredError(GovernanceError):
    status = 403


class PublishGateError(GovernanceError):
    status = 412


# --------------------------------------------------------------------------

class Governance:
    def __init__(self, db: Database, provider: ModelProvider):
        self.db = db
        self.provider = provider
        self._wlock = threading.RLock()

    # -- helpers -----------------------------------------------------------

    def _audit(self, conn, entity, entity_id, action, actor, detail=None):
        conn.execute(
            "INSERT INTO audit_log(entity,entity_id,action,actor,detail_json,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (entity, entity_id, action, actor, json.dumps(detail or {}, ensure_ascii=False), now()),
        )

    def _get_template_row(self, conn, template_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM templates WHERE template_id=?", (template_id,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"template {template_id} not found")
        return row

    def _get_version_row(self, conn, version_hash: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM template_versions WHERE version_hash=?", (version_hash,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"version {version_hash} not found")
        return row

    def _latest_version_row(self, conn, template_id: str) -> sqlite3.Row | None:
        # rowid is monotonic insertion order => the newest version of the
        # template (semver ordering is validated at insert time).
        return conn.execute(
            "SELECT * FROM template_versions WHERE template_id=?"
            " ORDER BY rowid DESC LIMIT 1",
            (template_id,),
        ).fetchone()

    def _latest_published_row(self, conn, template_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM template_versions WHERE template_id=? AND status='published'"
            " ORDER BY published_at DESC, rowid DESC LIMIT 1",
            (template_id,),
        ).fetchone()

    @staticmethod
    def version_body(row: sqlite3.Row) -> dict:
        return {
            "system_instructions": row["system_instructions"],
            "variables": jloads(row["variables_json"]),
            "tools": jloads(row["tools_json"]),
            "model_params": jloads(row["model_params_json"]),
            "model_id": row["model_id"],
        }

    @staticmethod
    def version_public(row: sqlite3.Row) -> dict:
        d = row_to_dict(row)
        for k in ("variables_json", "tools_json", "model_params_json",
                  "bump_basis_json", "evidence_summary_json"):
            d[k.replace("_json", "")] = jloads(d.pop(k))
        d.pop("system_instructions", None)
        d["body"] = {
            "system_instructions": row["system_instructions"],
            "variables": d["variables"],
            "tools": d["tools"],
            "model_params": d["model_params"],
            "model_id": row["model_id"],
        }
        return d

    # -- templates ---------------------------------------------------------

    def create_template(self, *, name: str, actor: str, description: str = "") -> dict:
        if not name or not str(name).strip():
            raise ValidationError("name required")
        tid = new_id("tpl")
        ts = now()
        with self._wlock:
            conn = self.db.conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO templates(template_id,name,description,created_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (tid, name.strip(), description, actor, ts, ts),
                )
                self._audit(conn, "template", tid, "create", actor, {"name": name})
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get_template(tid)

    def get_template(self, template_id: str, *, with_versions: bool = True) -> dict:
        conn = self.db.conn()
        row = self._get_template_row(conn, template_id)
        out = row_to_dict(row)
        if with_versions:
            rows = conn.execute(
                "SELECT * FROM template_versions WHERE template_id=? ORDER BY rowid",
                (template_id,),
            ).fetchall()
            out["versions"] = [self.version_public(r) for r in rows]
        active = conn.execute(
            "SELECT * FROM active_deployment WHERE template_id=?", (template_id,)
        ).fetchone()
        if active:
            v = self._get_version_row(conn, active["version_hash"])
            out["active"] = {
                "version_hash": active["version_hash"],
                "semver": v["semver"],
                "activated_at": active["activated_at"],
                "actor": active["actor"],
            }
        else:
            out["active"] = None
        return out

    def list_templates(self) -> list[dict]:
        conn = self.db.conn()
        out = []
        for r in conn.execute("SELECT * FROM templates ORDER BY created_at"):
            d = self.get_template(r["template_id"], with_versions=False)
            vcount = conn.execute(
                "SELECT COUNT(*) c FROM template_versions WHERE template_id=?",
                (r["template_id"],),
            ).fetchone()["c"]
            d["version_count"] = vcount
            out.append(d)
        return out

    # -- versions ----------------------------------------------------------

    def create_version(
        self,
        template_id: str,
        body: dict,
        actor: str,
        *,
        semver: str | None = None,
    ) -> dict:
        body = canonical.normalize_body(body)
        chash = canonical.content_hash(body)

        with self._wlock:
            conn = self.db.conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._get_template_row(conn, template_id)

                dup = conn.execute(
                    "SELECT version_hash,semver FROM template_versions"
                    " WHERE template_id=? AND content_hash=?",
                    (template_id, chash),
                ).fetchone()
                if dup:
                    raise ConflictError(
                        f"identical content already versioned as {dup['semver']} "
                        f"({dup['version_hash']})"
                    )

                prev = self._latest_version_row(conn, template_id)
                prev_body = self.version_body(prev) if prev else None
                basis = classify_change(prev_body, body)
                klass = basis["class"]
                if semver is None:
                    semver = next_semver(prev["semver"] if prev else None, klass)
                else:
                    self._validate_semver_order(prev, semver)

                vhash = canonical.version_hash(template_id, semver, chash, actor)
                gshash = canonical.grants_snapshot_hash(body["tools"])

                expansion = None
                if prev_body is not None:
                    added = sorted(set(body["tools"]) - set(prev_body["tools"]))
                    if added:
                        expansion = {
                            "added": added,
                            "from_hash": canonical.grants_snapshot_hash(prev_body["tools"]),
                            "to_hash": gshash,
                        }

                conn.execute(
                    """INSERT INTO template_versions(
                        template_id,semver,content_hash,version_hash,
                        system_instructions,variables_json,tools_json,model_params_json,
                        model_id,model_fingerprint_at_creation,prev_version_hash,
                        change_class,bump_basis_json,grants_snapshot_hash,expansion_of_hash,
                        status,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        template_id, semver, chash, vhash,
                        body["system_instructions"],
                        json.dumps(body["variables"], ensure_ascii=False),
                        json.dumps(body["tools"], ensure_ascii=False),
                        json.dumps(body["model_params"], ensure_ascii=False),
                        body["model_id"],
                        self.provider.fingerprint(body["model_id"]),
                        prev["version_hash"] if prev else None,
                        klass,
                        json.dumps(basis, ensure_ascii=False),
                        gshash,
                        prev["version_hash"] if expansion else None,
                        "draft", actor, now(),
                    ),
                )
                conn.execute(
                    "UPDATE templates SET updated_at=? WHERE template_id=?",
                    (now(), template_id),
                )
                self._audit(conn, "version", vhash, "create", actor,
                            {"semver": semver, "class": klass, "reasons": basis["reasons"]})
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get_version(vhash)

    @staticmethod
    def _validate_semver_order(prev, semver: str) -> None:
        parts = semver.split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValidationError("semver must look like MAJOR.MINOR.PATCH")
        if prev is None:
            return
        cur = tuple(int(p) for p in parts)
        old = tuple(int(p) for p in prev["semver"].split("."))
        if cur <= old:
            raise ConflictError(f"semver {semver} must be greater than {prev['semver']}")

    def get_version(self, version_hash: str) -> dict:
        conn = self.db.conn()
        row = self._get_version_row(conn, version_hash)
        out = self.version_public(row)
        ev = conn.execute(
            "SELECT * FROM evaluations WHERE version_hash=? ORDER BY started_at",
            (version_hash,),
        ).fetchall()
        out["evaluations"] = [
            {
                "eval_id": r["eval_id"], "status": r["status"], "late": bool(r["late"]),
                "testset_id": r["testset_id"], "testset_hash": r["testset_hash"],
                "evidence_hash": r["evidence_hash"],
                "summary": jloads(r["summary_json"]),
                "started_at": r["started_at"], "finished_at": r["finished_at"],
            }
            for r in ev
        ]
        apps = conn.execute(
            "SELECT * FROM approvals WHERE version_hash=? ORDER BY created_at", (version_hash,)
        ).fetchall()
        out["approvals"] = [self._approval_public(r) for r in apps]
        return out

    def list_versions(self, template_id: str) -> list[dict]:
        conn = self.db.conn()
        self._get_template_row(conn, template_id)
        rows = conn.execute(
            "SELECT * FROM template_versions WHERE template_id=? ORDER BY rowid",
            (template_id,),
        ).fetchall()
        return [self.version_public(r) for r in rows]

    def versions_diff(self, a_hash: str, b_hash: str) -> dict:
        conn = self.db.conn()
        a, b = self._get_version_row(a_hash), self._get_version_row(b_hash)
        if a["template_id"] != b["template_id"]:
            raise ValidationError("versions belong to different templates")
        return {
            "a": {"version_hash": a_hash, "semver": a["semver"]},
            "b": {"version_hash": b_hash, "semver": b["semver"]},
            "a_content_hash": a["content_hash"],
            "b_content_hash": b["content_hash"],
            "template_changed": a["content_hash"] != b["content_hash"],
            "diff": body_diff(self.version_body(a), self.version_body(b)),
        }

    # -- approvals ---------------------------------------------------------

    @staticmethod
    def _approval_public(row: sqlite3.Row) -> dict:
        d = row_to_dict(row)
        d["grants_expansion"] = jloads(d.pop("grants_expansion_json"))
        return d

    def request_approval(
        self,
        version_hash: str,
        kind: str,
        actor: str,
        reason: str = "",
    ) -> dict:
        if kind not in ("publish", "permission_expansion"):
            raise ValidationError("kind must be publish or permission_expansion")
        with self._wlock:
            conn = self.db.conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                v = self._get_version_row(conn, version_hash)
                grants_exp = None
                snap_hash = None
                if kind == "permission_expansion":
                    if not v["expansion_of_hash"]:
                        raise StateError(
                            "permission_expansion approval only valid when the version"
                            " adds tool grants over the previous version"
                        )
                    prev = self._get_version_row(conn, v["expansion_of_hash"])
                    prev_grants = jloads(prev["tools_json"])
                    added = sorted(set(jloads(v["tools_json"])) - set(prev_grants))
                    if not added:
                        raise StateError("no new tool grants found; expansion approval not needed")
                    snap_hash = v["grants_snapshot_hash"]
                    grants_exp = {
                        "added": added,
                        "from_version": prev["version_hash"],
                        "from_semver": prev["semver"],
                        "from_grants_snapshot_hash": canonical.grants_snapshot_hash(prev_grants),
                        "to_hash": snap_hash,
                    }
                aid = new_id("apr")
                conn.execute(
                    """INSERT INTO approvals(
                        approval_id,template_id,version_hash,kind,requested_by,status,
                        reason,grants_snapshot_hash,grants_expansion_json,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (aid, v["template_id"], version_hash, kind, actor, "requested",
                     reason, snap_hash, json.dumps(grants_exp, ensure_ascii=False), now()),
                )
                self._audit(conn, "approval", aid, "request", actor,
                            {"version": version_hash, "kind": kind})
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self._approval_public(
            conn.execute("SELECT * FROM approvals WHERE approval_id=?", (aid,)).fetchone()
        )

    def decide_approval(self, approval_id: str, approve: bool, actor: str, note: str = "") -> dict:
        with self._wlock:
            conn = self.db.conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
                ).fetchone()
                if not row:
                    raise NotFoundError(f"approval {approval_id} not found")
                if row["status"] != "requested":
                    raise StateError(f"approval already {row['status']}")
                status = "approved" if approve else "rejected"
                conn.execute(
                    "UPDATE approvals SET status=?,decided_by=?,decided_at=? WHERE approval_id=?",
                    (status, actor, now(), approval_id),
                )
                if approve and row["kind"] == "publish":
                    conn.execute(
                        "UPDATE template_versions SET status='pending' WHERE version_hash=?",
                        (row["version_hash"],),
                    )
                self._audit(conn, "approval", approval_id, status, actor, {"note": note})
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self._approval_public(
            conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        )

    def list_approvals(self, *, status: str | None = None, version_hash: str | None = None):
        conn = self.db.conn()
        q = "SELECT * FROM approvals WHERE 1=1"
        args: list[Any] = []
        if status:
            q += " AND status=?"; args.append(status)
        if version_hash:
            q += " AND version_hash=?"; args.append(version_hash)
        q += " ORDER BY created_at"
        return [self._approval_public(r) for r in conn.execute(q, args)]

    # -- publish gate ------------------------------------------------------

    def _check_publish_gate(self, conn, v: sqlite3.Row) -> dict:
        """Return the pinned evidence record, or raise PublishGateError/403."""
        errors = []

        publish_apr = conn.execute(
            "SELECT * FROM approvals WHERE version_hash=? AND kind='publish' AND status='approved'",
            (v["version_hash"],),
        ).fetchone()
        if not publish_apr:
            errors.append("missing approved 'publish' approval")

        if v["expansion_of_hash"]:
            exp = conn.execute(
                "SELECT * FROM approvals WHERE version_hash=? "
                "AND kind='permission_expansion' AND status='approved'",
                (v["version_hash"],),
            ).fetchone()
            if not exp:
                raise ApprovalRequiredError(
                    "tool permission expansion requires an approved "
                    "permission_expansion approval for this exact version"
                )
            if exp["grants_snapshot_hash"] != v["grants_snapshot_hash"]:
                raise ApprovalRequiredError(
                    "grants snapshot drifted after expansion approval; re-approval required"
                )

        ev = conn.execute(
            "SELECT * FROM evaluations WHERE version_hash=? AND status='complete' AND late=0"
            " ORDER BY finished_at DESC, rowid DESC LIMIT 1",
            (v["version_hash"],),
        ).fetchone()
        if not ev:
            errors.append("no completed evaluation evidence on the fixed test set")
            raise PublishGateError({"errors": errors})
        summary = jloads(ev["summary_json"])
        if summary["pass_rate"] < PASS_RATE_GATE:
            errors.append(
                f"pass_rate {summary['pass_rate']:.2f} below gate {PASS_RATE_GATE:.2f}"
            )
        if errors:
            raise PublishGateError({"errors": errors, "evidence": summary})
        return {"eval": ev, "summary": summary}

    def publish_version(self, version_hash: str, actor: str) -> dict:
        with self._wlock:
            conn = self.db.conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                v = self._get_version_row(conn, version_hash)
                if v["status"] == "published":
                    raise StateError("version already published")
                if v["status"] == "rejected":
                    raise StateError("version was rejected")
                gate = self._check_publish_gate(conn, v)
                ev = gate["eval"]
                conn.execute(
                    """UPDATE template_versions
                       SET status='published', published_at=?, published_by=?,
                           testset_id=?, evidence_hash=?, evidence_summary_json=?
                     WHERE version_hash=?""",
                    (now(), actor, ev["testset_id"], ev["evidence_hash"],
                     json.dumps(gate["summary"], ensure_ascii=False), version_hash),
                )
                # Any not-yet-finished or later evaluations become late evidence.
                conn.execute(
                    "UPDATE evaluations SET late=1, publish_ref=? WHERE version_hash=? "
                    "AND eval_id<>? AND (status='running' OR late=0)",
                    (ev["evidence_hash"], version_hash, ev["eval_id"]),
                )
                self._audit(conn, "version", version_hash, "publish", actor,
                            {"evidence_hash": ev["evidence_hash"], "testset": ev["testset_id"]})
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get_version(version_hash)

    # -- deployments -------------------------------------------------------

    def activate_version(
        self,
        template_id: str,
        version_hash: str,
        actor: str,
        reason: str = "",
        *,
        expected_active: str | None = None,
        action: str = "activate",
    ) -> dict:
        with self._wlock:
            conn = self.db.conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._get_template_row(conn, template_id)
                v = self._get_version_row(conn, version_hash)
                if v["template_id"] != template_id:
                    raise ValidationError("version does not belong to template")
                if v["status"] != "published":
                    raise StateError(f"only published versions can serve traffic (got {v['status']})")

                cur = conn.execute(
                    "SELECT * FROM active_deployment WHERE template_id=?", (template_id,)
                ).fetchone()
                current_hash = cur["version_hash"] if cur else None
                if expected_active is not None and expected_active != current_hash:
                    raise ConflictError(
                        f"active pointer changed concurrently: expected {expected_active}, "
                        f"found {current_hash}"
                    )
                if current_hash == version_hash:
                    raise ConflictError("that version is already active")

                conn.execute(
                    """INSERT INTO active_deployment(template_id,version_hash,activated_at,actor)
                       VALUES(?,?,?,?)
                       ON CONFLICT(template_id) DO UPDATE SET
                         version_hash=excluded.version_hash,
                         activated_at=excluded.activated_at,
                         actor=excluded.actor""",
                    (template_id, version_hash, now(), actor),
                )
                conn.execute(
                    "INSERT INTO deployments(template_id,version_hash,action,from_version_hash,"
                    "reason,actor,created_at) VALUES(?,?,?,?,?,?,?)",
                    (template_id, version_hash, action, current_hash, reason, actor, now()),
                )
                self._audit(conn, "deployment", template_id, action, actor,
                            {"from": current_hash, "to": version_hash})
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get_template(template_id)

    def rollback(self, template_id: str, version_hash: str, actor: str, reason: str = "",
                 *, expected_active: str | None = None) -> dict:
        return self.activate_version(
            template_id, version_hash, actor, reason or "rollback",
            expected_active=expected_active, action="rollback",
        )

    def get_active_version(self, template_id: str) -> dict:
        conn = self.db.conn()
        cur = conn.execute(
            "SELECT * FROM active_deployment WHERE template_id=?", (template_id,)
        ).fetchone()
        if not cur:
            raise StateError(f"template {template_id} has no active deployment")
        v = self._get_version_row(conn, cur["version_hash"])
        out = self.version_public(v)
        out["activated_at"] = cur["activated_at"]
        out["activated_by"] = cur["actor"]
        return out

    def list_deployments(self, template_id: str) -> list[dict]:
        conn = self.db.conn()
        self._get_template_row(conn, template_id)
        return [dict(r) for r in conn.execute(
            "SELECT * FROM deployments WHERE template_id=? ORDER BY id", (template_id,))]

    # -- audit -------------------------------------------------------------

    def recent_audit(self, limit: int = 100) -> list[dict]:
        conn = self.db.conn()
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["detail"] = jloads(d.pop("detail_json"))
            out.append(d)
        return out

"""Replay / re-verification service with three-axis attribution.

Given a recorded online request we can independently determine whether:

  INPUT    : the request inputs differ (digest compare)
  MODEL    : the model provider changed under the same model_id (fingerprint
             compare -> silent upstream weight revision)
  TEMPLATE : the template version live now differs from the one that served
             the request (version_hash / content_hash compare, with field diff)

The replay is re-executed in a chosen mode:
  recorded : exact recorded version body + recorded inputs, current provider
  active   : current active version + recorded inputs
If none of the three axes moved yet the output changed, that is
UNEXPECTED_DRIFT (non-determinism outside our provenance surface).
"""
from __future__ import annotations

import json

from . import canonical
from .diffing import body_diff
from .evaluation import validate_inputs
from .governance import Governance, new_id, now
from .models import ModelProvider
from .store import Database, jloads

AXES = ("INPUT", "MODEL", "TEMPLATE")


class ReplayService:
    def __init__(self, db: Database, provider: ModelProvider, gov: Governance):
        self.db = db
        self.provider = provider
        self.gov = gov

    def replay(self, request_id: str, *, mode: str = "recorded",
               inputs_override: dict | None = None) -> dict:
        if mode not in ("recorded", "active"):
            from .governance import ValidationError
            raise ValidationError("mode must be 'recorded' or 'active'")

        conn = self.db.conn()
        rrow = conn.execute(
            "SELECT * FROM requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if not rrow:
            from .governance import NotFoundError
            raise NotFoundError(f"request {request_id} not found")

        recorded_version = self.gov._get_version_row(conn, rrow["version_hash"])
        recorded_body = self.gov.version_body(recorded_version)
        recorded_inputs = jloads(rrow["inputs_json"])

        # --- INPUT axis ----------------------------------------------------
        replay_inputs = inputs_override if inputs_override is not None else recorded_inputs
        replay_inputs = validate_inputs(recorded_body["variables"], replay_inputs)
        replay_digest = canonical.input_digest(replay_inputs)
        input_changed = replay_digest != rrow["input_digest"]
        input_detail = {
            "recorded_digest": rrow["input_digest"],
            "replay_digest": replay_digest,
        }
        if input_changed:
            input_detail["recorded_inputs"] = recorded_inputs
            input_detail["replay_inputs"] = replay_inputs

        # --- TEMPLATE axis -------------------------------------------------
        active_row = conn.execute(
            "SELECT * FROM active_deployment WHERE template_id=?", (rrow["template_id"],)
        ).fetchone()
        active_hash = active_row["version_hash"] if active_row else None
        template_detail: dict = {
            "recorded_version_hash": rrow["version_hash"],
            "recorded_semver": rrow["semver"],
            "active_version_hash": active_hash,
        }
        template_changed = active_hash != rrow["version_hash"]
        if active_hash:
            active_version = self.gov._get_version_row(conn, active_hash)
            template_detail["active_semver"] = active_version["semver"]
            template_detail["content_changed"] = (
                active_version["content_hash"] != recorded_version["content_hash"]
            )
            template_detail["diff"] = body_diff(recorded_body, self.gov.version_body(active_version))
        else:
            template_detail["note"] = "template has no active deployment anymore"

        # --- MODEL axis ----------------------------------------------------
        try:
            current_fp = self.provider.fingerprint(rrow["model_id"])
            current_rev = self.provider.revision(rrow["model_id"])
        except KeyError:
            current_fp, current_rev = None, None
        model_changed = current_fp != rrow["model_fingerprint"]
        model_detail = {
            "model_id": rrow["model_id"],
            "recorded_revision": rrow["model_revision"],
            "current_revision": current_rev,
            "recorded_fingerprint": rrow["model_fingerprint"],
            "current_fingerprint": current_fp,
        }

        axis_map = {
            "INPUT": {"changed": input_changed, "detail": input_detail},
            "MODEL": {"changed": model_changed, "detail": model_detail},
            "TEMPLATE": {"changed": template_changed, "detail": template_detail},
        }
        axes_changed = [a for a in AXES if axis_map[a]["changed"]]

        # --- re-execute ----------------------------------------------------
        exec_version = active_version if (mode == "active" and active_hash) else recorded_version
        exec_body = self.gov.version_body(exec_version)
        # Validate replay inputs against the executed version's contract too.
        exec_inputs = validate_inputs(exec_body["variables"], replay_inputs)
        verdict_note = ""
        try:
            resp = self.provider.complete(
                model_id=exec_body["model_id"],
                system_instructions=exec_body["system_instructions"],
                variables=exec_inputs,
                model_params=exec_body["model_params"],
            )
            replay_output = resp.text
            replay_fps = {
                "model_id": resp.model_id,
                "model_revision": resp.revision,
                "model_fingerprint": resp.fingerprint,
                "version_hash": exec_version["version_hash"],
                "content_hash": exec_version["content_hash"],
                "prompt_sha": resp.prompt_sha,
            }
        except KeyError as exc:
            replay_output = None
            verdict_note = f"replay failed: {exc}"
            replay_fps = {"error": verdict_note}

        output_changed = replay_output != rrow["output_text"]
        reproduced = not output_changed and replay_output is not None

        # Only axes that actually fed THIS replay execution can explain its
        # output. A moved deployment pointer is informational while replaying
        # the *recorded* version (provenance review of an old request).
        relevant: dict[str, bool] = {
            "INPUT": input_changed,
            "MODEL": model_changed and exec_body["model_id"] == rrow["model_id"],
            "TEMPLATE": (
                exec_version["version_hash"] != rrow["version_hash"]
                or exec_version["content_hash"] != recorded_version["content_hash"]
            ),
        }
        relevant_changed = [a for a in AXES if relevant[a]]
        axis_map["_relevant_axes"] = relevant_changed
        axis_map["_mode"] = mode

        if replay_output is None:
            verdict = "failed"
        elif not relevant_changed and reproduced:
            verdict = "exact_reproduction"
        elif not relevant_changed and output_changed:
            verdict = "unexpected_drift"
        elif relevant_changed and reproduced:
            verdict = "changed_but_stable"
        else:
            verdict = "changed_output"

        # Primary axis = most likely cause when several moved, in precedence.
        primary = None
        if output_changed or verdict == "failed":
            for a in ("MODEL", "INPUT", "TEMPLATE"):
                if relevant[a]:
                    primary = a
                    break

        replay_id = new_id("rep")
        original_fps = {
            "model_id": rrow["model_id"],
            "model_revision": rrow["model_revision"],
            "model_fingerprint": rrow["model_fingerprint"],
            "version_hash": rrow["version_hash"],
            "content_hash": recorded_version["content_hash"],
            "prompt_sha": rrow["prompt_sha"],
        }
        conn.execute(
            """INSERT INTO replays(
                replay_id,request_id,axes_json,axis_detail_json,reproduced,
                output_changed,original_output,replay_output,
                original_fingerprints_json,replay_fingerprints_json,
                verdict,note,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (replay_id, request_id,
             json.dumps(relevant_changed), json.dumps(axis_map, ensure_ascii=False),
             1 if reproduced else 0, 1 if output_changed else 0,
             rrow["output_text"], replay_output,
             json.dumps(original_fps, ensure_ascii=False),
             json.dumps(replay_fps, ensure_ascii=False),
             verdict, verdict_note, now()),
        )

        return {
            "replay_id": replay_id,
            "request_id": request_id,
            "mode": mode,
            "axes": axis_map,
            "axes_changed": axes_changed,
            "relevant_axes_changed": relevant_changed,
            "primary_axis": primary,
            "reproduced": reproduced,
            "output_changed": output_changed,
            "verdict": verdict,
            "note": verdict_note,
            "original": {
                "output": rrow["output_text"],
                "created_at": rrow["created_at"],
                "fingerprints": original_fps,
            },
            "replay": {
                "output": replay_output,
                "at": now(),
                "fingerprints": replay_fps,
            },
            "created_at": now(),
        }

    def get_replay(self, replay_id: str) -> dict:
        conn = self.db.conn()
        row = conn.execute("SELECT * FROM replays WHERE replay_id=?", (replay_id,)).fetchone()
        if not row:
            from .governance import NotFoundError
            raise NotFoundError(f"replay {replay_id} not found")
        return self._public(row)

    def list_replays(self, request_id: str | None = None) -> list[dict]:
        conn = self.db.conn()
        if request_id:
            rows = conn.execute(
                "SELECT * FROM replays WHERE request_id=? ORDER BY rowid", (request_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM replays ORDER BY rowid DESC LIMIT 200").fetchall()
        return [self._public(r) for r in rows]

    @staticmethod
    def _public(row) -> dict:
        d = dict(row)
        d["axes_changed"] = jloads(d.pop("axes_json"))
        d["axes"] = jloads(d.pop("axis_detail_json"))
        d["original_fingerprints"] = jloads(d.pop("original_fingerprints_json"))
        d["replay_fingerprints"] = jloads(d.pop("replay_fingerprints_json"))
        d["reproduced"] = bool(row["reproduced"])
        d["output_changed"] = bool(row["output_changed"])
        return d

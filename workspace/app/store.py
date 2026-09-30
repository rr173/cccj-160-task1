"""SQLite storage layer with the full governance schema."""
from __future__ import annotations

import json
import sqlite3
import threading
import pathlib

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS templates (
    template_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Immutable version rows. Only lifecycle status / evidence snapshot columns
-- ever change, and only via controlled transitions.
CREATE TABLE IF NOT EXISTS template_versions (
    template_id        TEXT NOT NULL REFERENCES templates(template_id),
    semver             TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    version_hash       TEXT PRIMARY KEY,
    system_instructions TEXT NOT NULL,
    variables_json     TEXT NOT NULL,
    tools_json         TEXT NOT NULL,
    model_params_json  TEXT NOT NULL,
    model_id           TEXT NOT NULL,
    model_fingerprint_at_creation TEXT NOT NULL,
    prev_version_hash  TEXT,
    change_class       TEXT NOT NULL,            -- major|minor|patch|initial
    bump_basis_json    TEXT NOT NULL,
    grants_snapshot_hash TEXT NOT NULL,
    expansion_of_hash  TEXT,                    -- version this expands upon
    status             TEXT NOT NULL DEFAULT 'draft', -- draft|pending|published|rejected
    testset_id         TEXT,
    evidence_hash      TEXT,                    -- snapshot pinned at publish
    evidence_summary_json TEXT,
    published_at       TEXT,
    published_by       TEXT,
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    UNIQUE(template_id, semver),
    UNIQUE(template_id, content_hash)
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id        TEXT PRIMARY KEY,
    template_id        TEXT NOT NULL,
    version_hash       TEXT NOT NULL REFERENCES template_versions(version_hash),
    kind               TEXT NOT NULL,           -- publish|permission_expansion
    requested_by       TEXT NOT NULL,
    decided_by         TEXT,
    status             TEXT NOT NULL DEFAULT 'requested', -- requested|approved|rejected
    reason             TEXT NOT NULL DEFAULT '',
    grants_snapshot_hash TEXT,                  -- expansion only
    grants_expansion_json TEXT,                 -- {added:[...], from_hash, to_hash}
    created_at         TEXT NOT NULL,
    decided_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_version ON approvals(version_hash);

CREATE TABLE IF NOT EXISTS testsets (
    testset_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    frozen       INTEGER NOT NULL DEFAULT 0,
    frozen_at    TEXT,
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_cases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    testset_id   TEXT NOT NULL REFERENCES testsets(testset_id),
    case_id      TEXT NOT NULL,
    inputs_json  TEXT NOT NULL,
    expect_json  TEXT NOT NULL,
    UNIQUE(testset_id, case_id)
);

-- Evaluations run against an immutable frozen-snapshot of a test set.
CREATE TABLE IF NOT EXISTS evaluations (
    eval_id       TEXT PRIMARY KEY,
    template_id   TEXT NOT NULL,
    version_hash  TEXT NOT NULL REFERENCES template_versions(version_hash),
    testset_id    TEXT NOT NULL REFERENCES testsets(testset_id),
    testset_hash  TEXT NOT NULL,              -- snapshot captured at eval time
    status        TEXT NOT NULL DEFAULT 'running', -- running|complete|failed
    late          INTEGER NOT NULL DEFAULT 0, -- arrived after publish
    publish_ref   TEXT,                       -- evidence_hash of publish (if late)
    error         TEXT,
    summary_json  TEXT,
    evidence_hash TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_evals_version ON evaluations(version_hash);

CREATE TABLE IF NOT EXISTS eval_results (
    eval_id      TEXT NOT NULL REFERENCES evaluations(eval_id),
    case_id      TEXT NOT NULL,
    inputs_json  TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    output_text  TEXT NOT NULL,
    passed       INTEGER NOT NULL,
    score        REAL NOT NULL,
    failure_json TEXT,
    model_id     TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    model_fingerprint TEXT NOT NULL,
    prompt_sha   TEXT NOT NULL,
    PRIMARY KEY(eval_id, case_id)
);

-- Append-only deployment ledger. The partial unique index guarantees exactly
-- one live pointer per template, switched inside one transaction.
CREATE TABLE IF NOT EXISTS deployments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id     TEXT NOT NULL,
    version_hash    TEXT NOT NULL REFERENCES template_versions(version_hash),
    action          TEXT NOT NULL,             -- activate|rollback
    from_version_hash TEXT,
    reason          TEXT NOT NULL DEFAULT '',
    actor           TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deploy_tpl ON deployments(template_id, id);

CREATE TABLE IF NOT EXISTS active_deployment (
    template_id  TEXT PRIMARY KEY REFERENCES templates(template_id),
    version_hash TEXT NOT NULL REFERENCES template_versions(version_hash),
    activated_at TEXT NOT NULL,
    actor        TEXT NOT NULL
);

-- Every online inference is bound to exact version/model/input fingerprints.
CREATE TABLE IF NOT EXISTS requests (
    request_id     TEXT PRIMARY KEY,
    template_id    TEXT NOT NULL,
    version_hash   TEXT NOT NULL,
    semver         TEXT NOT NULL,
    model_id       TEXT NOT NULL,
    model_fingerprint TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    inputs_json    TEXT NOT NULL,
    input_digest   TEXT NOT NULL,
    output_text    TEXT NOT NULL,
    params_json    TEXT NOT NULL,
    prompt_sha     TEXT NOT NULL,
    status_code    TEXT NOT NULL DEFAULT '200',
    error          TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_template ON requests(template_id, created_at);

CREATE TABLE IF NOT EXISTS replays (
    replay_id      TEXT PRIMARY KEY,
    request_id     TEXT NOT NULL REFERENCES requests(request_id),
    axes_json      TEXT NOT NULL,             -- [INPUT, MODEL, ...]
    axis_detail_json TEXT NOT NULL,
    reproduced     INTEGER NOT NULL,
    output_changed INTEGER NOT NULL,
    original_output TEXT NOT NULL,
    replay_output  TEXT,
    original_fingerprints_json TEXT NOT NULL,
    replay_fingerprints_json   TEXT NOT NULL,
    verdict        TEXT NOT NULL,             -- exact_reproduction|...|failed
    note           TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_replays_request ON replays(request_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity      TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    action      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        if path != ":memory:":
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: each request thread gets its own connection.
        self._master = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self._master.row_factory = sqlite3.Row
        self._master.execute("PRAGMA journal_mode=WAL")
        self._master.execute("PRAGMA foreign_keys=ON")
        self._master.executescript(SCHEMA)
        self._tls = threading.local()
        self._mem = path == ":memory:"

    def conn(self) -> sqlite3.Connection:
        if self._mem:
            # In-memory DB is shared via the single master connection; it is
            # serialized by a global lock in governance for multi-statement
            # transactions (see StoreTx).
            return self._master
        c = getattr(self._tls, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=5000")
            self._tls.conn = c
        return c

    def close(self) -> None:
        self._master.close()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def jloads(s: str | None, default=None):
    if s is None:
        return default
    return json.loads(s)

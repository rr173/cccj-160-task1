"""JSON HTTP API + embedded HTML pages (stdlib only).

Run:  python -m app.server --db data/governance.db --port 8080
Pages:
  /                     console / template registry
  /evaluate.html        evaluation page
  /replay.html          replay report page
"""
from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .governance import (
    ApprovalRequiredError,
    ConflictError,
    GovernanceError,
    NotFoundError,
    PublishGateError,
    StateError,
    ValidationError,
)
from .service import Service, default_db_path
from .webpages import PAGES

_JSON_CT = "application/json; charset=utf-8"


class ApiHandler(BaseHTTPRequestHandler):
    server_version = f"PromptGov/{__version__}"
    service: Service = None  # set on the server instance

    # -- helpers -----------------------------------------------------------

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", _JSON_CT)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValidationError("request body must be valid JSON")
        if not isinstance(data, dict):
            raise ValidationError("request body must be a JSON object")
        return data

    def _query(self) -> dict:
        return {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}

    def log_message(self, fmt, *args):  # quieter, structured logs
        qs = urlparse(self.path).query
        self.server.logline(f"{self.command} {urlparse(self.path).path}"
                            f"{'?' + qs if qs else ''} -> {args[1] if len(args) > 1 else ''}")

    # -- dispatch ----------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        path = urlparse(self.path).path.rstrip("/") or "/"
        svc = self.server.service
        try:
            if method == "GET" and (path == "/" or path.endswith((".html", ".css", ".js"))):
                return self._serve_page(path)
            if path == "/health":
                return self._json({"status": "ok", "version": __version__})
            if path == "/api/meta":
                return self._json({
                    "spec": "gov-v1",
                    "pass_rate_gate": 1.0,
                    "axes": ["INPUT", "MODEL", "TEMPLATE"],
                    "models": svc.provider.list_models(),
                })

            handler = self._match(method, path)
            if handler is None:
                return self._json({"error": "not found", "path": path}, 404)
            fn, kwargs, query = handler
            data = self._read_json() if method == "POST" else {}
            import inspect
            if "query" in inspect.signature(fn).parameters:
                result = fn(data, query=query, **kwargs) if method == "POST" else fn(query=query, **kwargs)
            else:
                result = fn(data, **kwargs) if method == "POST" else fn(**kwargs)
            return self._json(result, 200)
        except (ValidationError, NotFoundError, ConflictError, StateError,
                ApprovalRequiredError, PublishGateError, GovernanceError) as exc:
            return self._json(
                {"error": type(exc).__name__, "message": str(exc),
                 "detail": getattr(exc, "args", [None])[0] if isinstance(getattr(exc, "args", [None])[0], dict) else None},
                exc.status,
            )
        except KeyError as exc:
            return self._json({"error": "KeyError", "message": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            self.server.logline(f"ERROR {method} {path}: {exc!r}")
            return self._json({"error": "internal", "message": repr(exc)}, 500)

    def _serve_page(self, path: str):
        name = "index.html" if path == "/" else path.lstrip("/")
        content, ctype = PAGES.get(name, (None, None))
        if content is None:
            return self._json({"error": "page not found"}, 404)
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routing table -----------------------------------------------------

    def _match(self, method: str, path: str):
        svc = self.server.service
        g, ev, srv, rep = svc.gov, svc.evals, svc.serving, svc.replay
        routes: list[tuple[str, str, str, object]] = [
            ("GET", "/api/templates", "list", g.list_templates),
            ("POST", "/api/templates", "create", _create_template(g)),
            ("GET", r"/api/templates/(?P<template_id>[\w-]+)", "get", g.get_template),
            ("POST", r"/api/templates/(?P<template_id>[\w-]+)/versions", "version", _create_version(g)),
            ("GET", r"/api/templates/(?P<template_id>[\w-]+)/versions", "versions", g.list_versions),
            ("POST", r"/api/templates/(?P<template_id>[\w-]+)/activate", "activate", _activate(g)),
            ("POST", r"/api/templates/(?P<template_id>[\w-]+)/rollback", "rollback", _rollback(g)),
            ("GET", r"/api/templates/(?P<template_id>[\w-]+)/deployments", "deploys", g.list_deployments),
            ("POST", r"/api/templates/(?P<template_id>[\w-]+)/infer", "infer", _infer(srv)),

            ("GET", r"/api/versions/(?P<version_hash>[0-9a-f]+)", "vget", g.get_version),
            ("POST", r"/api/versions/(?P<version_hash>[0-9a-f]+)/approvals", "appreq", _request_approval(g)),
            ("POST", r"/api/versions/(?P<version_hash>[0-9a-f]+)/publish", "publish", _publish(g)),
            ("POST", r"/api/versions/(?P<version_hash>[0-9a-f]+)/evaluate", "eval", _start_eval(ev)),
            ("GET", r"/api/versions/(?P<version_hash>[0-9a-f]+)/diff", "vdiff", _version_diff(g)),

            ("GET", r"/api/versions/diff/(?P<a>[0-9a-f]+)/(?P<b>[0-9a-f]+)", "diffab", g.versions_diff),

            ("GET", "/api/approvals", "appls", g.list_approvals),
            ("POST", r"/api/approvals/(?P<approval_id>apr_[\w]+)/decision", "appdec", _decide_approval(g)),

            ("GET", "/api/testsets", "tsls", ev.list_testsets),
            ("POST", "/api/testsets", "tscreate", _create_testset(ev)),
            ("GET", r"/api/testsets/(?P<testset_id>set_[\w-]+)", "tsget", ev.get_testset),
            ("POST", r"/api/testsets/(?P<testset_id>set_[\w-]+)/freeze", "tsfreeze", _freeze(ev)),

            ("GET", "/api/evaluations", "evls", ev.list_evaluations),
            ("GET", r"/api/evaluations/(?P<eval_id>eval_[\w-]+)", "evget", ev.get_evaluation),
            ("POST", "/api/evaluations/finish", "evfin", _finish(ev)),

            ("GET", "/api/requests", "reqls", srv.list_requests),
            ("GET", r"/api/requests/(?P<request_id>req_[\w-]+)", "reqget", srv.get_request),
            ("POST", r"/api/requests/(?P<request_id>req_[\w-]+)/replay", "replay", _replay(rep)),

            ("GET", "/api/replays", "reps", rep.list_replays),
            ("GET", r"/api/replays/(?P<replay_id>rep_[\w-]+)", "repget", rep.get_replay),

            ("GET", "/api/audit", "audit", _audit(g)),
            ("GET", "/api/models", "models", lambda: {"models": svc.provider.list_models()}),
            ("POST", "/api/models/rotate", "rotate", _rotate(svc)),
        ]
        for m, pat, _name, fn in routes:
            if m != method:
                continue
            mm = re.fullmatch(pat, path)
            if mm:
                return (fn, mm.groupdict(), self._query())
        return None


# -- thin body adapters (validation + actor defaults live in domain) --------

def _actor(data: dict, default: str = "api-user") -> str:
    return str(data.get("actor") or default)


def _create_template(g: Governance):
    def fn(data: dict):
        return g.create_template(
            name=data.get("name", ""), actor=_actor(data),
            description=data.get("description", ""),
        )
    return fn


def _create_version(g: Governance):
    def fn(data: dict, template_id: str):
        semver = data.get("semver")
        return g.create_version(
            template_id,
            {
                "system_instructions": data.get("system_instructions"),
                "variables": data.get("variables"),
                "tools": data.get("tools"),
                "model_params": data.get("model_params"),
                "model_id": data.get("model_id"),
            },
            _actor(data, "author"),
            semver=semver,
        )
    return fn


def _request_approval(g: Governance):
    def fn(data: dict, version_hash: str):
        return g.request_approval(
            version_hash, data.get("kind", "publish"), _actor(data, "requester"),
            reason=data.get("reason", ""),
        )
    return fn


def _decide_approval(g: Governance):
    def fn(data: dict, approval_id: str):
        approve = bool(data.get("approve"))
        return g.decide_approval(approval_id, approve, _actor(data, "approver"),
                                 note=data.get("note", ""))
    return fn


def _publish(g: Governance):
    def fn(data: dict, version_hash: str):
        return g.publish_version(version_hash, _actor(data, "release-manager"))
    return fn


def _start_eval(ev: EvaluationService):
    def fn(data: dict, version_hash: str):
        tsid = data.get("testset_id")
        if not tsid:
            raise ValidationError("testset_id required")
        return ev.start_evaluation(
            version_hash, tsid, actor=_actor(data, "ci"),
            delay=float(data.get("delay", 0) or 0),
            background=bool(data.get("background", True)),
        )
    return fn


def _create_testset(ev: EvaluationService):
    def fn(data: dict):
        return ev.create_testset(
            data.get("name", ""), data.get("cases", []),
            _actor(data, "qa"), description=data.get("description", ""),
        )
    return fn


def _freeze(ev: EvaluationService):
    def fn(data: dict, testset_id: str):
        return ev.freeze_testset(testset_id)
    return fn


def _activate(g: Governance):
    def fn(data: dict, template_id: str):
        return g.activate_version(
            template_id, data["version_hash"], _actor(data, "ops"),
            reason=data.get("reason", "hot update"),
            expected_active=data.get("expected_active"),
        )
    return fn


def _rollback(g: Governance):
    def fn(data: dict, template_id: str):
        return g.rollback(
            template_id, data["version_hash"], _actor(data, "ops"),
            reason=data.get("reason", "rollback"),
            expected_active=data.get("expected_active"),
        )
    return fn


def _infer(srv):
    def fn(data: dict, template_id: str):
        if "inputs" not in data:
            raise ValidationError("inputs required")
        return srv.infer(template_id, data["inputs"], actor=_actor(data, "client"))
    return fn


def _replay(rep):
    def fn(data: dict, request_id: str):
        return rep.replay(
            request_id, mode=data.get("mode", "recorded"),
            inputs_override=data.get("inputs"),
        )
    return fn


def _version_diff(g: Governance):
    def fn(version_hash: str, query: dict):
        other = query.get("other")
        if not other:
            raise ValidationError("?other=<version_hash> required")
        return g.versions_diff(version_hash, other)
    fn.__wants_query__ = True
    return fn


def _finish(ev: EvaluationService):
    def fn(data: dict):
        remaining = ev.finish_latest_evaluations(timeout=float(data.get("timeout", 10)))
        return {"running_remaining": remaining}
    return fn


def _audit(g: Governance):
    def fn(query: dict):
        return {"events": g.recent_audit(limit=int(query.get("limit", 100)))}
    fn.__wants_query__ = True
    return fn


def _rotate(svc: Service):
    def fn(data: dict):
        model_id = data.get("model_id")
        new_rev = data.get("revision")
        if not model_id or not new_rev:
            raise ValidationError("model_id and revision required")
        fp = svc.provider.rotate(model_id, new_rev)
        return {"model_id": model_id, "active_revision": new_rev, "fingerprint": fp}
    return fn


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, service: Service, quiet: bool = False):
        self.service = service
        self._quiet = quiet
        self._log_lock = threading.Lock()
        super().__init__(addr, ApiHandler)

    def logline(self, line: str):
        if self._quiet:
            return
        with self._log_lock:
            print(line, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=default_db_path())
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    service = Service(args.db)
    server = ApiServer((args.host, args.port), service, quiet=args.quiet)
    print(f"prompt governance service on http://{args.host}:{args.port} (db={args.db})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()

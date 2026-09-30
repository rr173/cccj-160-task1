"""Embedded web pages (evaluation console, replay report, registry)."""
from __future__ import annotations

import pathlib

_WEB = pathlib.Path(__file__).resolve().parent / "web"


def _load() -> dict[str, tuple[str, str]]:
    pages = {}
    ctypes = {".html": "text/html; charset=utf-8",
              ".js": "application/javascript; charset=utf-8",
              ".css": "text/css; charset=utf-8"}
    for f in _WEB.rglob("*"):
        if f.is_file() and f.suffix in ctypes:
            pages[f.name] = (f.read_text(encoding="utf-8"), ctypes[f.suffix])
    return pages


PAGES = _load()

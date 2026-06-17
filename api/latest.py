"""Vercel serverless function: read stored odds snapshots from Turso.

GET /api/latest
  (default)                 most recent 'featured' snapshot (parsed payload)
  ?scope=event&event_id=ID  most recent stored blob for one match
  ?list=true&limit=50       recent snapshot metadata (no payloads)

Reads only — costs 0 the-odds-api credits. Stdlib only, no dependencies.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler


class AppError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _turso_base() -> str:
    url = os.environ.get("TURSO_DATABASE_URL")
    if not url:
        raise AppError(500, "TURSO_DATABASE_URL is not set in Vercel env vars.")
    url = url.strip().rstrip("/")
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    return url


def _arg(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    return {"type": "text", "value": str(value)}


def turso_query(sql: str, args: list) -> list:
    """Run one SELECT and return a list of row dicts."""
    token = os.environ.get("TURSO_AUTH_TOKEN")
    if not token:
        raise AppError(500, "TURSO_AUTH_TOKEN is not set in Vercel env vars.")
    body = json.dumps({
        "requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": [_arg(a) for a in args]}},
            {"type": "close"},
        ]
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{_turso_base()}/v2/pipeline",
        data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AppError(502, f"Turso HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise AppError(502, f"Could not reach Turso: {e.reason}")

    results = out.get("results", [])
    if not results or results[0].get("type") == "error":
        msg = (results[0].get("error", {}).get("message")
               if results else "no result")
        # A missing table means nothing has been stored yet.
        if msg and "no such table" in msg.lower():
            return []
        raise AppError(502, f"Turso error: {msg}")

    result = results[0]["response"]["result"]
    cols = [c["name"] for c in result.get("cols", [])]
    rows = []
    for raw in result.get("rows", []):
        rows.append({cols[i]: _cell(raw[i]) for i in range(len(cols))})
    return rows


def _cell(cell):
    t = cell.get("type")
    v = cell.get("value")
    if t == "null":
        return None
    if t == "integer":
        return int(v)
    if t == "float":
        return float(v)
    return v


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

            if qs.get("list", ["false"])[0].lower() in ("1", "true", "yes"):
                limit = min(int(qs.get("limit", ["50"])[0]), 200)
                rows = turso_query(
                    "SELECT id, fetched_at, scope, event_id, markets, "
                    "credits_remaining, credits_cost FROM odds_snapshots "
                    "ORDER BY id DESC LIMIT ?",
                    [limit],
                )
                return self._send(200, {"count": len(rows), "snapshots": rows})

            scope = qs.get("scope", ["featured"])[0]
            if scope == "event":
                event_id = qs.get("event_id", [""])[0]
                if not event_id:
                    raise AppError(422, "event_id is required when scope=event.")
                rows = turso_query(
                    "SELECT * FROM odds_snapshots WHERE scope='event' "
                    "AND event_id=? ORDER BY id DESC LIMIT 1",
                    [event_id],
                )
            else:
                rows = turso_query(
                    "SELECT * FROM odds_snapshots WHERE scope='featured' "
                    "ORDER BY id DESC LIMIT 1",
                    [],
                )

            if not rows:
                return self._send(404, {"error": "No snapshot stored yet. "
                                        "Call /api/refresh first."})
            row = rows[0]
            payload = row.pop("payload", None)
            self._send(200, {
                "meta": row,
                "payload": json.loads(payload) if payload else None,
            })
        except AppError as e:
            self._send(e.status, {"error": e.message})
        except Exception as e:
            self._send(500, {"error": f"Unexpected: {e}"})

    def _send(self, status: int, obj: dict):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "s-maxage=30")
        self.end_headers()
        self.wfile.write(body)

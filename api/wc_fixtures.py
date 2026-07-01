"""Vercel serverless function: read stored World Cup 2026 fixtures + results.

GET /api/wc_fixtures
  -> { ok, count, updated_at, fixtures: [ ... ] }

Reads the `wc_matches` table populated by /api/refresh_wc (daily cron). Costs no
external API credits. If nothing has been stored yet it returns an empty list.
Stdlib only, no dependencies.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

# API-Football status.short codes that mean the match has finished.
FINISHED = {"FT", "AET", "PEN"}
# ... that mean it is currently being played.
LIVE = {"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT"}


class AppError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _turso_base():
    url = os.environ.get("TURSO_DATABASE_URL")
    if not url:
        raise AppError(500, "TURSO_DATABASE_URL is not set in Vercel env vars.")
    url = url.strip().rstrip("/")
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    return url


def _arg(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    return {"type": "text", "value": str(v)}


def _cell(cell):
    t, v = cell.get("type"), cell.get("value")
    if t == "null":
        return None
    if t == "integer":
        return int(v)
    if t == "float":
        return float(v)
    return v


def turso_query(sql, args):
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
        f"{_turso_base()}/v2/pipeline", data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AppError(502, f"Turso HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise AppError(502, f"Could not reach Turso: {e.reason}")

    results = out.get("results", [])
    if not results or results[0].get("type") == "error":
        msg = (results[0].get("error", {}).get("message") if results else "no result")
        # No table yet -> nothing stored; treat as empty rather than an error.
        if msg and "no such table" in msg.lower():
            return []
        raise AppError(502, f"Turso error: {msg}")

    result = results[0]["response"]["result"]
    cols = [c["name"] for c in result.get("cols", [])]
    return [{col: _cell(val) for col, val in zip(cols, raw)}
            for raw in result.get("rows", [])]


def to_fixture(r):
    status = r.get("status_short")
    if status in FINISHED:
        state = "finished"
    elif status in LIVE:
        state = "live"
    else:
        state = "upcoming"
    return {
        "fixture_id": r.get("fixture_id"),
        "date": r.get("match_date"),
        "round": r.get("round"),
        "status": status,
        "status_long": r.get("status_long"),
        "state": state,
        "home_team": r.get("home_team"),   # may be null -> TBD
        "away_team": r.get("away_team"),
        "home_goals": r.get("home_goals"),
        "away_goals": r.get("away_goals"),
        "venue": r.get("venue_name"),
        "city": r.get("venue_city"),
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, response = 200, {}
        try:
            rows = turso_query(
                "SELECT fixture_id, match_date, timestamp, status_short, "
                "status_long, round, home_team, away_team, home_goals, "
                "away_goals, venue_name, venue_city, fetched_at "
                "FROM wc_matches "
                "ORDER BY timestamp IS NULL, timestamp ASC, match_date ASC",
                [],
            )
            fixtures = [to_fixture(r) for r in rows]
            updated_at = max((r.get("fetched_at") or "" for r in rows), default=None)
            response = {
                "ok": True,
                "count": len(fixtures),
                "updated_at": updated_at,
                "fixtures": fixtures,
            }
        except AppError as e:
            status, response = e.status, {"ok": False, "error": e.message}
        except Exception as e:
            status, response = 500, {"ok": False, "error": f"Unexpected: {e}"}
        self._send(status, response)

    def _send(self, status, obj):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "s-maxage=300, stale-while-revalidate=600")
        self.end_headers()
        self.wfile.write(body)

"""Vercel function: World Cup fixtures (the-odds-api), stored in Turso.

GET  /api/fixtures             -> list stored fixtures + whether odds retrieved (0 credits)
POST /api/fixtures?key=SECRET  -> fetch fixtures from the-odds-api (free) and store

Stdlib only, no dependencies.
"""

import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

BASE_URL = "https://api.the-odds-api.com/v4"
WORLD_CUP_KEY = "soccer_fifa_world_cup"

CREATE_FIXTURES = """
CREATE TABLE IF NOT EXISTS fixtures (
  event_id      TEXT PRIMARY KEY,
  sport         TEXT,
  home_team     TEXT,
  away_team     TEXT,
  commence_time TEXT,
  fetched_at    TEXT
)
"""
CREATE_ODDS = """
CREATE TABLE IF NOT EXISTS fixture_odds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id          TEXT NOT NULL,
  bookmaker         TEXT,
  regions           TEXT,
  markets           TEXT,
  credits_cost      INTEGER,
  credits_remaining INTEGER,
  fetched_at        TEXT NOT NULL,
  payload           TEXT NOT NULL
)
"""


class AppError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --- the-odds-api ----------------------------------------------------------

def odds_get(path, params):
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise AppError(500, "ODDS_API_KEY is not set in Vercel env vars.")
    params = dict(params)
    params["apiKey"] = key
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "gamblor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AppError(e.code, f"odds-api {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise AppError(502, f"Could not reach the odds API: {e.reason}")
    except ValueError as e:
        raise AppError(502, f"Invalid JSON from the odds API: {e}")


# --- Turso -----------------------------------------------------------------

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
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    return {"type": "text", "value": str(v)}


def _cell(c):
    t, v = c.get("type"), c.get("value")
    if t == "null":
        return None
    if t == "integer":
        return int(v)
    if t == "float":
        return float(v)
    return v


def turso(statements):
    """Run [(sql, [args]), ...]; return list of row-dict lists per statement."""
    token = os.environ.get("TURSO_AUTH_TOKEN")
    if not token:
        raise AppError(500, "TURSO_AUTH_TOKEN is not set in Vercel env vars.")
    reqs = [{"type": "execute", "stmt": {"sql": s, "args": [_arg(a) for a in args]}}
            for s, args in statements]
    reqs.append({"type": "close"})
    body = json.dumps({"requests": reqs}).encode("utf-8")
    req = urllib.request.Request(
        f"{_turso_base()}/v2/pipeline", data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise AppError(502, f"Turso {e.code}: {e.read().decode('utf-8','replace')[:200]}")
    except urllib.error.URLError as e:
        raise AppError(502, f"Could not reach Turso: {e.reason}")
    except ValueError as e:
        raise AppError(502, f"Invalid JSON from Turso: {e}")

    parsed = []
    for r in out.get("results", []):
        if r.get("type") == "error":
            raise AppError(502, f"Turso error: {r.get('error', {}).get('message')}")
        res = (r.get("response") or {}).get("result") or {}
        cols = [c["name"] for c in res.get("cols", [])]
        parsed.append([{col: _cell(val) for col, val in zip(cols, raw)}
                       for raw in res.get("rows", [])])
    return parsed


# --- handler ---------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, response = 200, {}
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            results = turso([
                (CREATE_FIXTURES, []),
                (CREATE_ODDS, []),
                ("SELECT f.event_id, f.home_team, f.away_team, f.commence_time, "
                 "(SELECT COUNT(*) FROM fixture_odds o WHERE o.event_id = f.event_id) "
                 "AS odds_count FROM fixtures f ORDER BY f.commence_time ASC", []),
            ])
            rows = results[2]
            fixtures = [{
                "event_id": r["event_id"],
                "home_team": r["home_team"],
                "away_team": r["away_team"],
                "commence_time": r["commence_time"],
                "has_odds": (r["odds_count"] or 0) > 0,
            } for r in rows]
            response = {"ok": True, "count": len(fixtures), "fixtures": fixtures}
        except AppError as e:
            status, response = e.status, {"ok": False, "error": e.message}
        except Exception as e:
            status, response = 500, {"ok": False, "error": f"Unexpected: {e}"}
        self._send(status, response)

    def do_POST(self):
        status, response = 200, {}
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._authorize(qs)
            events = odds_get(f"/sports/{WORLD_CUP_KEY}/events", {"dateFormat": "iso"})
            if not isinstance(events, list):
                events = []
            now = datetime.now(timezone.utc).isoformat()
            stmts = [(CREATE_FIXTURES, []), (CREATE_ODDS, [])]
            for e in events:
                if not isinstance(e, dict):
                    continue
                stmts.append((
                    "INSERT OR REPLACE INTO fixtures "
                    "(event_id, sport, home_team, away_team, commence_time, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [e.get("id"), WORLD_CUP_KEY, e.get("home_team"),
                     e.get("away_team"), e.get("commence_time"), now],
                ))
            turso(stmts)
            response = {"ok": True, "stored": len(stmts) - 2}
        except AppError as e:
            status, response = e.status, {"ok": False, "error": e.message}
        except Exception as e:
            status, response = 500, {"ok": False, "error": f"Unexpected: {e}"}
        self._send(status, response)

    def _authorize(self, qs):
        secret = os.environ.get("CRON_SECRET")
        if not secret:
            return
        header = self.headers.get("Authorization", "")
        provided = header[7:] if header.lower().startswith("bearer ") else qs.get("key", [""])[0]
        if not hmac.compare_digest(provided, secret):
            raise AppError(401, "Unauthorized: missing or wrong access key.")

    def _send(self, status, obj):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
